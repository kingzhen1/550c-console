"""离线自测：把 core.plugin 换成假模块，导入 main.py 后实际跑一遍报告生成与告警评估。

KiraAI 环境不在这台机器上，但报告/告警/采样这些逻辑是纯函数式的，
可以用构造数据直接验证，避免「装上才发现字段名对不上」。
"""
import json
import os
import sys
import types
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---- 1. 造一个最小可用的 core.plugin -------------------------------------
core = types.ModuleType("core")
plugin = types.ModuleType("core.plugin")


class _BasePlugin:
    def __init__(self, ctx=None, cfg=None):
        self.ctx = ctx
        self.plugin_cfg = cfg or {}


class _PluginContext:
    pass


class _PageMenu:
    def __init__(self, **kw):
        self.kw = kw


class _PluginPage:
    @staticmethod
    def from_folder(_p):
        return None


def _register(*_a, **_kw):
    def deco(fn):
        return fn
    return deco


_register.api = _register
_register.page = _register

import logging
plugin.BasePlugin = _BasePlugin
plugin.PluginContext = _PluginContext
plugin.PageMenu = _PageMenu
plugin.PluginPage = _PluginPage
plugin.register = _register
plugin.logger = logging.getLogger("550c")
core.plugin = plugin
sys.modules["core"] = core
sys.modules["core.plugin"] = plugin

# ---- 2. 导入被测模块 ------------------------------------------------------
spec = importlib.util.spec_from_file_location("main550c", os.path.join(ROOT, "main.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fail = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not ok:
        fail.append(name)


# ---- 3. 归并键：数字噪声应被抹平 ------------------------------------------
k1 = mod._err_key("napcat", "WebSocket 重连失败：[Errno 111] Connect call failed ('127.0.0.1', 3001) 59次")
k2 = mod._err_key("napcat", "WebSocket 重连失败：[Errno 111] Connect call failed ('127.0.0.1', 3001) 42次")
check("同类报错归并成同一键", k1 == k2, k1[:60])
check("归并键抹掉了数字", "<n>" in k1 or "<ip>" in k1)
check("不同来源不误并", mod._err_key("other", "x") != mod._err_key("napcat", "x"))

# ---- 4. 告警评估 ----------------------------------------------------------
inst = mod.Boot550CPlugin.__new__(mod.Boot550CPlugin)
inst.plugin_cfg = {}
inst._online_since = {}
inst._last_sample_at = 0
inst._alert_sent = {}

status = {
    "ts": 1793500000,
    "version": "1.7.1",
    "host": {"ok": True, "cpu": 12.0, "cores": 8, "mem": 93.5, "mem_used": 10.2,
             "mem_total": 11.2, "disk": 97.2, "boot_s": 113472000, "proc_s": 3600, "proc_mem": 96.5},
    "tokens": {"ok": True, "calls": 468, "success": 468, "input": 21488479, "output": 1061043,
               "cached": 19455884, "total": 22549522, "avg_ms": 948.8, "currency": "CNY",
               "cost": 3.2145, "unknown_price": [],
               "by_model": [
                   {"model": "deepseek-v4-flash", "calls": 300, "input": 12000000, "output": 600000,
                    "cached": 9000000, "cost": 2.1},
                   {"model": "some-unpriced", "calls": 168, "input": 9488479, "output": 461043,
                    "cached": 10455884, "cost": None},
               ]},
    "adapters": [
        {"id": "qq1", "name": "QQ-主号", "enabled": True, "live": True, "socket": False,
         "logged_in": False, "dead": False, "online_s": 120},
        {"id": "qq2", "name": "QQ-备用", "enabled": True, "live": True, "socket": True,
         "logged_in": True, "dead": False, "online_s": 999},
    ],
    "models": {},
    "plugins": {},
}

alerts = inst._evaluate_alerts(status)
ids = [a["id"] for a in alerts]
check("磁盘越线触发告警", "host-disk" in ids, str(ids))
check("内存越线触发告警", "host-mem" in ids)
check("CPU 未越线不告警", "host-cpu" not in ids)
check("ws 断开触发告警", "link-socket-qq1" in ids)
check("正常链路不告警", "link-socket-qq2" not in ids)
check("磁盘超过阈值+5 判为 bad", any(a["id"] == "host-disk" and a["lv"] == "bad" for a in alerts))
check("内存贴阈值判为 warn", any(a["id"] == "host-mem" and a["lv"] == "warn" for a in alerts))

# 总开关关掉就不该有任何告警
inst.plugin_cfg = {"alert_enabled": False}
check("告警总开关可关闭", inst._evaluate_alerts(status) == [])
inst.plugin_cfg = {}

# ---- 5. 报告生成 ----------------------------------------------------------
md = inst._build_report(status, 1)
check("报告非空", len(md) > 500, f"{len(md)} 字符")
check("报告含主机段", "## 主机" in md)
check("报告含趋势段", "## 趋势" in md)
check("报告含用量段", "## 用量（24 小时）" in md)
check("用量读到了 input 字段", "21,488,479" in md, "（若为 0 说明字段名又对不上）")
check("用量读到了 output 字段", "1,061,043" in md)
check("金额保留四位", "¥3.2145" in md)
check("按模型明细存在", "### 按模型" in md)
check("未配价模型有提示", "未配价" in md)
check("报告含告警段", "## 告警" in md)
check("报告含报错段", "## 报错" in md)
check("Markdown 表格没被截断", md.count("| --- |") >= 2)

# ---- 6. 采样：写入 / 读回 / 截止时间过滤 ---------------------------------
import tempfile
from pathlib import Path

tmpdir = tempfile.mkdtemp()
inst.__class__._SAMPLE_NAME = os.path.join(tmpdir, "samples.jsonl")
inst._last_sample_at = 0
inst._append_sample(status)
inst._last_sample_at = 0
inst._append_sample(status)
rows = inst._load_samples(24)
check("采样能写入并读回", len(rows) >= 1, f"{len(rows)} 点")
check("采样点带时间戳", bool(rows and rows[0].get("t")))
check("采样点带磁盘值", bool(rows and rows[0].get("disk") == 97.2))
before = len(inst._load_samples(24))
inst._append_sample(status)  # 不重置节流时间戳，立刻再写应被挡住
check("15 秒节流生效", len(inst._load_samples(24)) == before, f"{before} -> {len(inst._load_samples(24))}")

# 伪造一条两小时前的旧点，验证时间窗过滤
sample_path = inst._sample_path()
old_row = {"t": int(__import__("time").time()) - 7200, "cpu": 1, "mem": 2, "disk": 3, "tok": 4}
with open(sample_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(old_row) + "\n")
check("1 小时窗口过滤掉旧点", all(p["t"] > old_row["t"] for p in inst._load_samples(1)))
check("3 小时窗口能读到旧点", any(p["t"] == old_row["t"] for p in inst._load_samples(3)))

# ---- 7. 诊断包：整份可序列化、可外发 -------------------------------------
import asyncio

inst.ctx = types.SimpleNamespace()   # 没有 adapter_mgr / db / plugin_mgr，走降级分支
pack = asyncio.run(inst.api_diag())
check("诊断包返回 ok", pack.get("status") == "ok")
d = pack.get("data") or {}
for key in ("generated_at", "plugin_version", "settings", "host", "adapters",
            "tokens", "alerts", "errors", "trend"):
    check(f"诊断包含 {key}", key in d)
check("诊断包剔除 by_model 明细", "by_model" not in (d.get("tokens") or {}))
check("诊断包可 JSON 序列化", isinstance(json.dumps(d, ensure_ascii=False), str))
check("诊断包趋势带采样点上限", len((d.get("trend") or {}).get("points") or []) <= 1200)
check("诊断包里有报错归并字段", "groups" in (d.get("errors") or {}))

# ---- 8. 自动更新：版本比较 / 只挑已装 / 保护名单 / 黑名单 ---------------
upd = mod.AutoUpdater(inst)
import importlib
_upd_mod = importlib.import_module(getattr(mod, "__name__", "main550c").replace("main550c", "updater")) \
    if False else sys.modules.get("updater")
if _upd_mod is None:
    # main 里用的是回落导入，updater 模块名就是 "updater"
    _upd_mod = importlib.import_module("updater")


def _fake_info(pid, ver, builtin=False):
    return types.SimpleNamespace(plugin_id=pid, version=ver, display_name=pid,
                                 builtin=builtin, hidden=False)


class _FakeMgr:
    plugin_dir = os.path.join(tmpdir, "plugins")

    def __init__(self, infos):
        self._infos = infos

    def list_plugins(self):
        return list(self._infos)


inst.ctx = types.SimpleNamespace(plugin_mgr=_FakeMgr([
    _fake_info("kira-ai-plugin-550c-boot", "1.8.0"),   # 自己：不能动
    _fake_info("kira_ops", "0.1.0"),                    # 备份器：不能动
    _fake_info("a-old", "1.0.0"),                       # 有新版：该换
    _fake_info("b-same", "2.0.0"),                      # 已最新：不换
    _fake_info("c-norepo", "1.0.0"),                    # 市场里有版本但没仓库地址：不换
    _fake_info("d-new-install", "0.0.0"),               # 本地没这个 id 的市场条目不算
    _fake_info("e-builtin", "1.0.0", builtin=True),      # 内置：不碰
]))
inst.plugin_cfg = {}
upd._state["blocked"] = {}

market = {
    "kira-ai-plugin-550c-boot": {"version": "9.9.9", "repo": "x/y"},
    "kira_ops": {"version": "9.9.9", "repo": "x/y"},
    "a-old": {"version": "1.1.0", "repo": "x/a-old"},
    "b-same": {"version": "2.0.0", "repo": "x/b-same"},
    "c-norepo": {"version": "1.5.0", "repo": ""},
    "e-builtin": {"version": "9.9.9", "repo": "x/y"},
}
plans = upd.plans(market)
ids = [p["id"] for p in plans]
check("只挑出真有新版的已装插件", ids == ["a-old"], str(ids))
check("自己不在候选里", "kira-ai-plugin-550c-boot" not in ids)
check("kira_ops 不在候选里", "kira_ops" not in ids)
check("没有仓库地址的不换", "c-norepo" not in ids)
check("内置插件不碰", "e-builtin" not in ids)
check("计划里带着 from/to", bool(plans) and plans[0]["from"] == "1.0.0" and plans[0]["to"] == "1.1.0")

# 排除名单生效
inst.plugin_cfg = {"update_exclude": "a-old"}
check("排除名单能拦住", upd.plans(market) == [])
inst.plugin_cfg = {}

# 连续失败到上限就拉黑，不再出现在候选里
upd._state["blocked"] = {"a-old": _upd_mod.FAIL_LIMIT}
check("失败满两次进黑名单", upd.plans(market) == [])
upd.unblock("a-old")
check("手动放出来后回到候选", [p["id"] for p in upd.plans(market)] == ["a-old"])
upd._state["blocked"] = {}

# 版本比较
check("1.10.0 大于 1.9.9", _upd_mod._newer("1.10.0", "1.9.9"))
check("相同版本不算新", not _upd_mod._newer("1.8.0", "v1.8.0"))
check("本地版本缺失时当新", _upd_mod._newer("1.0.0", "-"))
check("空的市场版本不算新", not _upd_mod._newer("", "1.0.0"))

# 总开关关掉时循环不该动手
inst.plugin_cfg = {"auto_update": False}
check("配置读到关闭", upd._cfg()["enabled"] is False)
check("间隔越界会被夹住", (inst.plugin_cfg.update({"update_interval_hours": 0}),
                          upd._cfg()["interval_h"] == 1)[1])
inst.plugin_cfg = {}

print()
if fail:
    print(f"失败 {len(fail)} 项：{fail}")
    sys.exit(1)
print("全部通过")
