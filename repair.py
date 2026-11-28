"""550C 报错自检修理 — 规则表 + 归并后的匹配。

这里只负责「这条报错该交给谁收拾」，不负责真的收拾。
真的动手在 main.py 里，因为它要摸到插件管理器、日志文件和看门狗。
这么拆的好处：规则能单独测，跑起来也不会因为 import 不到 Kira 就整个炸掉。

风险分级：
    low  —— 修了只会更好，最坏也就是没效果（重载插件、清采样、checkpoint）
    mid  —— 会动到运行时（拉起进程、删缓存），默认也修，但会记下来
    none —— 修不了，只给一句人话建议，别浪费按钮

一条报错命中多条规则时，取第一条 low → mid → none。
"""

from __future__ import annotations

import re

# fix 取值：wd_adapter / wd_kira / reload_plugin / trim_samples / db_checkpoint / clear_cache / none
RULES = [
    {
        "id": "adapter_ws",
        "name": "适配器/ws 断了",
        "risk": "low",
        "fix": "wd_adapter",
        "pat": r"(?i)(websocket|ws).{0,20}(close|closed|disconnect|refused)|napcat.{0,12}(断|失败|重连)|无法连接.{0,8}(napcat|适配器)",
        "tip": "语音链路那头的连接没了。看门狗会把适配器重起一遍，KiraAI 本身不用动。",
    },
    {
        "id": "conn_refused",
        "name": "端口不通",
        "risk": "low",
        "fix": "wd_adapter",
        "pat": r"(?i)connection refused|econnrefused|cannot connect to host|connect call failed|actively refused",
        "tip": "对方端口没人听。先修链路，再谈别的——这一类不修，后面一片报错都是它的回声。",
    },
    {
        "id": "plugin_load",
        "name": "插件没加载起来",
        "risk": "low",
        "fix": "reload_plugin",
        "pat": r"(?i)加载插件失败|failed to load plugin|plugininit|导入插件|plugin .{0,20}(load|init).{0,10}(error|failed)",
        "tip": "重载那个插件就行。重载完还是红的，说明是代码问题，不是运行时状态问题。",
    },
    {
        "id": "samples_huge",
        "name": "采样文件太大",
        "risk": "low",
        "fix": "trim_samples",
        "pat": r"(?i)samples\.jsonl|采样文件|trend",
        "tip": "趋势采样只留最近的，超了截掉旧的，不心疼。",
    },
    {
        "id": "db_locked",
        "name": "数据库被锁",
        "risk": "low",
        "fix": "db_checkpoint",
        "pat": r"(?i)database is locked|database table is locked|sqlite.{0,10}busy",
        "tip": "先做一次 WAL checkpoint 把写锁收掉，多数情况一下就通。",
    },
    {
        "id": "disk_full",
        "name": "盘满了",
        "risk": "mid",
        "fix": "clear_cache",
        "pat": r"(?i)no space left|enospc|磁盘.{0,6}(满|不足)|insufficient storage",
        "tip": "清媒体缓存里三天前的文件。清完还满，就该自己看大文件了。",
    },
    {
        "id": "too_many_files",
        "name": "句柄用光了",
        "risk": "mid",
        "fix": "wd_kira",
        "pat": r"(?i)too many open files|emfile",
        "tip": "只能重启主进程把句柄收回来。看门狗来干，别在面板里硬拉。",
    },
    {
        "id": "timeout",
        "name": "等回音等到超时",
        "risk": "none",
        "fix": "none",
        "pat": r"(?i)timeout|timed out|超时",
        "tip": "超时十次有九次是对端慢，不是你坏了。连着刷才值得动手，单次放过。",
    },
    {
        "id": "module_missing",
        "name": "缺依赖",
        "risk": "none",
        "fix": "none",
        "pat": r"(?i)modulenotfounderror|importerror|no module named",
        "tip": "少包。按报错里那个名字装上去，重载就好——这个按钮修不了，得装东西。",
    },
    {
        "id": "json_bad",
        "name": "拿到了烂数据",
        "risk": "none",
        "fix": "none",
        "pat": r"(?i)jsondecodeerror|expecting value|invalid json|unexpected token",
        "tip": "对端返回的不是 JSON。看是谁给的，不是靠重启能解决的。",
    },
    {
        "id": "auth_bad",
        "name": "身份没过",
        "risk": "none",
        "fix": "none",
        "pat": r"(?i)401|403|unauthorized|invalid api key|invalid token|forbidden",
        "tip": "key 错了或者过期了。去面板里那把 key 换掉，重试一百次也一样。",
    },
    {
        "id": "rate_limit",
        "name": "被限流",
        "risk": "none",
        "fix": "none",
        "pat": r"(?i)429|rate limit|too many requests",
        "tip": "人家让你慢点。等一会儿自己会好，这种报错不用修，也不用紧张。",
    },
    {
        "id": "benign_boot",
        "name": "启动期的自我打脸",
        "risk": "none",
        "fix": "none",
        "pat": r"(?i)deprecat|没有找到配置文件|config not found|will be removed",
        "tip": "它从来没影响过消息收发，属于纯自我打脸型报错，可以直接忽略。",
    },
]

_COMPILED = [(r, re.compile(r["pat"])) for r in RULES]


def match(group: dict):
    """给一条归并后的报错找规则。group 至少要有 head / src 字段。"""
    text = "%s %s" % (group.get("src") or "", group.get("head") or group.get("msg") or "")
    hits = [r for r, rx in _COMPILED if rx.search(text)]
    if not hits:
        return None
    for risk in ("low", "mid", "none"):
        for r in hits:
            if r["risk"] == risk:
                return r
    return hits[0]


def plan(groups) -> dict:
    """把归并清单翻成一张「哪些能修、修什么、谁去修」的单子。"""
    rows = []
    unknown = 0
    for g in groups or []:
        rule = match(g)
        if rule is None:
            unknown += 1
            rows.append({
                "key": g.get("key"), "head": g.get("head"), "src": g.get("src"),
                "n": g.get("n", 0), "lv": g.get("lv"),
                "rule": None, "name": "没见过的报错", "risk": "none", "fix": "none",
                "tip": "规则表里还没有这一类。看一眼堆栈，值当的话就补一条规则进去。",
            })
            continue
        rows.append({
            "key": g.get("key"), "head": g.get("head"), "src": g.get("src"),
            "n": g.get("n", 0), "lv": g.get("lv"),
            "rule": rule["id"], "name": rule["name"], "risk": rule["risk"],
            "fix": rule["fix"], "tip": rule["tip"],
        })
    fixable = [r for r in rows if r["fix"] != "none"]
    return {
        "rows": rows,
        "fixable": len(fixable),
        "auto": len([r for r in fixable if r["risk"] in ("low", "mid")]),
        "manual": len([r for r in rows if r["fix"] == "none"]),
        "unknown": unknown,
    }
