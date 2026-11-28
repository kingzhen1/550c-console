#!/usr/bin/env python3
"""550C 看门狗 — 独立于 KiraAI / 模型链路的最后一道防线。

存在的意义只有一个：当 QQ 全断、适配器全挂、甚至 KiraAI 主进程躺下的时候，
仍然有东西在跑，仍然能把东西拽起来。

所以这里不 import 任何 KiraAI 的东西，只用标准库。
它跑在一个独立终端里（Termux / Linux / macOS / Windows 都行），由 while 循环 / termux-job-scheduler / crond 拉起，
和面板、和模型、和 QQ 都没有依赖关系——它们全死了，这个还在。

用法：
    python watchdog.py --once        # 自检一轮，该修的修，然后退出
    python watchdog.py --loop        # 常驻，按 interval 轮转（termux 推荐）
    python watchdog.py --status      # 只打印上次结果，不动手
    python watchdog.py --init        # 生成一份默认配置

面板上那个「自检修」按钮干的事就是：往 trigger 文件里写个字。
本程序每轮开头看一眼 trigger，有就立刻跑一轮——这样按钮在没有 Kira 的时候也有意义。

配置：data/plugin_data/kira-ai-plugin-550c-boot/watchdog.json
状态：data/plugin_data/kira-ai-plugin-550c-boot/watchdog_state.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PLUGIN_ID = "kira-ai-plugin-550c-boot"

DEFAULT_CFG = {
    "enabled": True,
    "interval": 120,
    "kira_host": "127.0.0.1",
    "kira_port": 8080,
    "adapter_host": "127.0.0.1",
    "adapter_port": 3001,
    "timeout": 3,
    "kira_proc_pattern": "main.py",
    "restart_adapter": "",
    "restart_kira": "",
    "restart_kira_cmd": "",
    "max_retry_per_hour": 6,
    "cooldown": 180,
    "disk_warn": 95,
    "mem_warn": 95,
    "log_tail": 200,
    # 插件熔断：插件把聊天搞坏了就只停它。这里只负责判定+递条子，
    # 真正动手要 Kira 活着才做得到（改内存里的启用状态），所以写请求文件给它。
    "guard_auto": True,
    "guard_window_min": 20,
    "guard_min_errors": 3,
    "guard_cooldown_min": 60,
    "guard_whitelist": "kira-ai-plugin-550c-boot,default-chat,kira_session_plugin,"
                       "kira-ai-plugin-payload-defense,kira_persona_progression,"
                       "kira_accelerator,sustained-chat",
}


def state_dir() -> Path:
    env = os.environ.get("KIRA_DATA_DIR") or ""
    if env:
        return Path(env) / "plugin_data" / PLUGIN_ID
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "data" / "plugins").is_dir():
            return parent / "data" / "plugin_data" / PLUGIN_ID
    return here.parent / "_state"


def cfg_path() -> Path:
    return state_dir() / "watchdog.json"


def state_path() -> Path:
    return state_dir() / "watchdog_state.json"


def trigger_path() -> Path:
    return state_dir() / "watchdog_trigger"


def guard_req_path() -> Path:
    return state_dir() / "guard_requests.json"


def log_file_path() -> Path:
    """Kira 正在写的那份日志。state_dir 是 data/plugin_data/<id>，上两层就是 data。"""
    d = state_dir()
    for cand in (d.parents[1] / "log.log", d.parents[0] / "log.log"):
        try:
            if cand.is_file():
                return cand
        except Exception:
            continue
    return d.parents[1] / "log.log"


def runtime_env() -> dict:
    """认一下自己跑在哪儿。纯探测，不依赖任何平台专属命令。"""
    uname = (platform.system() or "").lower()
    hint = ""
    try:
        if os.environ.get("PREFIX", "").find("com.termux") >= 0 or \
           os.path.isdir("/data/data/com.termux"):
            key, hint = "termux", "termux · 独立进程"
        elif uname == "linux":
            if "microsoft" in (platform.release() or "").lower() or os.path.exists("/proc/version") and \
               "microsoft" in open("/proc/version").read().lower():
                key, hint = "wsl", "WSL · 独立进程"
            elif os.path.exists("/.dockerenv"):
                key, hint = "docker", "容器 · 独立进程"
            else:
                key, hint = "linux", "Linux · 独立进程"
        elif uname == "darwin":
            key, hint = "macos", "macOS · 独立进程"
        elif uname == "windows" or os.name == "nt":
            key, hint = "windows", "Windows · 独立进程"
        else:
            key, hint = uname or "unknown", (uname or "未知") + " · 独立进程"
    except Exception:
        key, hint = uname or "unknown", "独立进程"
    if hint and key not in ("unknown", ""):
        hint = hint.split(" · ")[0] + " · 独立进程"
    return {"key": key, "label": hint or "独立进程", "platform": platform.platform()}


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    p = cfg_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CFG})
        except Exception:
            pass
    return cfg


def save_json(p: Path, obj: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:
        print("[watchdog] 写文件失败 %s: %s" % (p, exc), file=sys.stderr)


def tcp_ok(host: str, port: int, timeout: float = 3.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, ""
    except Exception as exc:
        return False, "%s:%s %s" % (host, port, exc)


def proc_alive(pattern: str):
    """找进程：pgrep → ps → /proc → psutil，哪台机器有什么就用什么。"""
    if not pattern:
        return True, "未配置"
    # 1) pgrep（Linux / Termux / macOS）
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, timeout=5)
        pids = [x for x in out.stdout.decode("utf-8", "ignore").split() if x.strip()]
        if pids:
            return True, "pid %s" % ",".join(pids[:3])
    except Exception:
        pass
    # 2) ps 全表（Windows 没 pgrep，macOS 的正则也不总靠谱）
    for cmd in (["ps", "-A", "-o", "pid=,command="], ["ps", "-eo", "pid=,command="]):
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=6)
            hit = []
            for ln in out.stdout.decode("utf-8", "ignore").splitlines():
                ln = ln.strip()
                if not ln or pattern not in ln:
                    continue
                parts = ln.split(None, 1)
                if parts and parts[0].isdigit():
                    hit.append(parts[0])
            if hit:
                return True, "pid %s" % ",".join(hit[:3])
        except Exception:
            continue
    try:
        needle = pattern.encode()
        me = str(os.getpid())
        for d in Path("/proc").iterdir():
            if not d.name.isdigit() or d.name == me:
                continue
            try:
                cmd = (d / "cmdline").read_bytes().replace(b"\x00", b" ")
            except Exception:
                continue
            if needle in cmd and b"watchdog" not in cmd:
                return True, "pid %s" % d.name
    except Exception:
        pass
    try:
        import psutil
        hit = []
        for pr in psutil.process_iter(["pid", "cmdline"]):
            cl = " ".join(str(x) for x in (pr.info.get("cmdline") or []))
            if pattern in cl and "watchdog" not in cl:
                hit.append(str(pr.info.get("pid")))
        if hit:
            return True, "pid %s" % ",".join(hit[:3])
    except Exception:
        pass
    return False, "没找到含 %r 的进程" % pattern


def disk_mem() -> dict:
    out = {}
    root = os.path.abspath(os.sep)
    if os.name == "nt":
        root = os.environ.get("SystemDrive", "C:") + os.sep
    try:
        total, used, free = shutil.disk_usage(root)
        out["disk"] = round(used * 100.0 / total, 1)
        out["disk_free_gb"] = round(free / (1024 ** 3), 1)
    except Exception:
        pass
    try:
        info = {}
        for ln in Path("/proc/meminfo").read_text().splitlines()[:5]:
            k, v = ln.split(":", 1)
            info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal") or 0
        avail = info.get("MemAvailable") or info.get("MemFree") or 0
        if total:
            out["mem"] = round((total - avail) * 100.0 / total, 1)
    except Exception:
        pass
    # Windows / macOS 没有 /proc/meminfo，用 psutil 补
    if "mem" not in out:
        try:
            import psutil
            vm = psutil.virtual_memory()
            out["mem"] = round(float(vm.percent), 1)
        except Exception:
            pass
    return out


def load_state() -> dict:
    p = state_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"actions": [], "counters": {}}


def counter_allow(state: dict, key: str, cfg: dict):
    now = time.time()
    c = state.setdefault("counters", {}).setdefault(key, {"last": 0, "hour": 0, "n": 0})
    if now - float(c.get("hour", 0)) > 3600:
        c["hour"], c["n"] = now, 0
    if c.get("n", 0) >= int(cfg.get("max_retry_per_hour") or 6):
        return False, "该层级 1 小时内已重启 %s 次，暂停处理" % c.get("n")
    gap = now - float(c.get("last", 0))
    if gap < int(cfg.get("cooldown") or 180):
        return False, "冷却中，剩余 %d 秒" % int(int(cfg.get("cooldown") or 180) - gap)
    return True, ""


def bump_counter(state: dict, key: str) -> None:
    c = state.setdefault("counters", {}).setdefault(key, {"last": 0, "hour": 0, "n": 0})
    now = time.time()
    if now - float(c.get("hour", 0)) > 3600:
        c["hour"], c["n"] = now, 0
    c["last"] = now
    c["n"] = int(c.get("n", 0)) + 1


# ------------------------------------------------------------ 插件熔断

_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(ERROR|CRITICAL|WARNING)\s+\[([^\]]*)\]\s?(.*)$"
)
_STRONG = re.compile(
    r"(?i)traceback|exception|timeout|超时|提取|extract|崩溃|crash|"
    r"failed to call tool|tool_result: error|keyerror|attributeerror|typeerror|importerror"
)
_LOADFAIL = re.compile(r"(?i)failed to (initialize|load) plugin|加载插件失败|no module named")


def _plugin_ids() -> dict:
    """插件 id → 别名。只看目录名和 plugins.json，不依赖 Kira 活着。"""
    ids = set()
    data = state_dir().parents[1]
    try:
        pj = data / "config" / "plugins.json"
        if pj.is_file():
            ids |= {str(k) for k in json.loads(pj.read_text(encoding="utf-8")).keys()}
    except Exception:
        pass
    try:
        for d in (data / "plugins").iterdir():
            if d.is_dir() and not d.name.startswith((".", "__")):
                ids.add(d.name)
    except Exception:
        pass
    alias = {}
    for pid in ids:
        if not pid:
            continue
        short = re.sub(r"(?i)^kira[-_]ai[-_]plugin[-_]", "", pid)
        names = {pid, short, pid.lower(), short.lower()}
        alias[pid] = {n for n in names if len(n) >= 6}
    return alias


def _match_plugin(text: str, alias: dict) -> str:
    best, best_len = "", 0
    for pid, names in alias.items():
        for n in names:
            pat = r"(?<![A-Za-z0-9_\-])" + re.escape(n) + r"(?![A-Za-z0-9_\-])"
            if re.search(pat, text or "", re.I) and len(n) > best_len:
                best, best_len = pid, len(n)
    return best


def guard_scan(cfg: dict) -> dict:
    path = log_file_path()
    if not path.is_file():
        return {"ok": False, "msg": "日志不在：%s" % path, "rows": {}}
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return {"ok": False, "msg": "读日志失败：%s" % exc, "rows": {}}
    lines = raw.splitlines()[-15000:]

    alias = _plugin_ids()
    window = int(cfg.get("guard_window_min") or 20) * 60
    now = time.time()
    rows = {}
    for ln in lines:
        m = _LOG_RE.match(ln)
        if not m:
            continue
        date, hhmmss, lv, src, msg = m.groups()
        try:
            stamp = time.mktime(time.strptime(date + " " + hhmmss, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        age = now - stamp
        if age < -60 or age > window:
            continue
        text = "%s %s" % (src, msg)
        pid = _match_plugin(text, alias)
        if not pid:
            continue
        row = rows.setdefault(pid, {"pid": pid, "err": 0, "strong": 0, "loadfail": 0,
                                    "sample": "", "last": date + " " + hhmmss})
        row["last"] = date + " " + hhmmss
        if _LOADFAIL.search(text):
            row["loadfail"] += 1
            continue
        if lv in ("ERROR", "CRITICAL"):
            row["err"] += 1
            if _STRONG.search(text):
                row["strong"] += 1
                if not row["sample"]:
                    row["sample"] = (src + " " + msg)[:180]
    min_err = int(cfg.get("guard_min_errors") or 3)
    for row in rows.values():
        row["hot"] = bool(row["err"] >= min_err or row["strong"] >= 2
                          or row["loadfail"] >= max(4, min_err * 2))
    return {"ok": True, "rows": rows, "log": str(path)}


def guard_check(cfg: dict, state: dict) -> list:
    """判定 + 给 Kira 递条子。冷却记在 watchdog_state 里，绝不反复开关。"""
    if not cfg.get("guard_auto", True):
        return []
    wl = {x.strip() for x in str(cfg.get("guard_whitelist") or "").replace("，", ",").split(",") if x.strip()}
    wl.add(PLUGIN_ID)
    scan = guard_scan(cfg)
    if not scan.get("ok"):
        return []
    sent = state.setdefault("guard_sent", {})
    base = state.get("guard_baseline") or {}
    cooling = int(cfg.get("guard_cooldown_min") or 60) * 60
    now = time.time()
    out = []
    for pid, row in (scan.get("rows") or {}).items():
        if not row.get("hot") or pid in wl:
            continue
        prev = base.get(pid) or {}
        if prev.get("last") == row.get("last") and row["err"] <= int(prev.get("err") or 0):
            continue   # 还是启动时那一批，没长新的
        if now - float(sent.get(pid) or 0) < cooling:
            continue
        sent[pid] = now
        out.append({"pid": pid, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": "窗口内 %d 条 ERROR（强信号 %d）" % (row["err"], row["strong"]),
                    "errors": row["err"], "sample": row.get("sample") or ""})
    state["guard_baseline"] = {k: {"err": v.get("err"), "last": v.get("last")}
                               for k, v in (scan.get("rows") or {}).items()}
    if not out:
        return []
    p = guard_req_path()
    old = []
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            old = data.get("items") if isinstance(data, dict) else []
            old = old if isinstance(old, list) else []
    except Exception:
        old = []
    keep = [x for x in old if isinstance(x, dict) and now - float(x.get("_ts") or 0) < 3600]
    for item in out:
        item["_ts"] = now
        keep.append(item)
    save_json(p, {"items": keep, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return out


def run_cmd(cmd: str, timeout: int = 60):
    if not cmd:
        return False, "未配置命令"
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        ok = p.returncode == 0
        tail = (p.stdout + p.stderr).decode("utf-8", "ignore").strip().splitlines()
        return ok, (tail[-1][:200] if tail else "exit %s" % p.returncode)
    except subprocess.TimeoutExpired:
        return False, "命令超时"
    except Exception as exc:
        return False, str(exc)


def check_once(cfg: dict, do_fix: bool = True, source: str = "loop") -> dict:
    t0 = time.time()
    state = load_state()
    timeout = float(cfg.get("timeout") or 3)
    layers = {}

    kira_host = cfg.get("kira_host") or "127.0.0.1"
    kira_port = int(cfg.get("kira_port") or 0)
    ok, msg = tcp_ok(kira_host, kira_port, timeout) if kira_port else (False, "端口未配置")
    layers["kira_port"] = {"ok": ok, "msg": msg or "正常"}

    alive, pmsg = proc_alive(str(cfg.get("kira_proc_pattern") or ""))
    layers["kira_proc"] = {"ok": alive, "msg": pmsg}

    ad_host = cfg.get("adapter_host") or "127.0.0.1"
    ad_port = int(cfg.get("adapter_port") or 0)
    ok2, msg2 = tcp_ok(ad_host, ad_port, timeout) if ad_port else (False, "端口未配置")
    layers["adapter_port"] = {"ok": ok2, "msg": msg2 or "通"}

    res = disk_mem()
    if "disk" in res:
        layers["disk"] = {"ok": res["disk"] < float(cfg.get("disk_warn") or 92),
                          "msg": "%s%%（剩 %sG）" % (res["disk"], res.get("disk_free_gb", "?"))}
    if "mem" in res:
        layers["mem"] = {"ok": res["mem"] < float(cfg.get("mem_warn") or 95),
                         "msg": "%s%%" % res["mem"]}

    actions = []
    if do_fix:
        if not layers["adapter_port"]["ok"]:
            cmd = str(cfg.get("restart_adapter") or "")
            if cmd:
                allow, why = counter_allow(state, "adapter", cfg)
                if allow:
                    okr, orr = run_cmd(cmd)
                    bump_counter(state, "adapter")
                    actions.append({"t": time.strftime("%H:%M:%S"), "what": "重启适配器",
                                    "ok": okr, "msg": orr})
                else:
                    actions.append({"t": time.strftime("%H:%M:%S"), "what": "跳过适配器重启",
                                    "ok": False, "msg": why})
            else:
                actions.append({"t": time.strftime("%H:%M:%S"), "what": "适配器不通",
                                "ok": False, "msg": "配置里没写 restart_adapter，只能报不能修"})

        if not layers["kira_port"]["ok"] and not layers["kira_proc"]["ok"]:
            cmd = str(cfg.get("restart_kira_cmd") or cfg.get("restart_kira") or "")
            if cmd:
                allow, why = counter_allow(state, "kira", cfg)
                if allow:
                    okr, orr = run_cmd(cmd)
                    bump_counter(state, "kira")
                    actions.append({"t": time.strftime("%H:%M:%S"), "what": "拉起 KiraAI",
                                    "ok": okr, "msg": orr})
                else:
                    actions.append({"t": time.strftime("%H:%M:%S"), "what": "跳过 Kira 重启",
                                    "ok": False, "msg": why})
            else:
                actions.append({"t": time.strftime("%H:%M:%S"), "what": "Kira 不在",
                                "ok": False, "msg": "配置里没写 restart_kira，只能报不能修"})

    # 插件熔断：第三层，和端口/进程都不冲突。这里只判定+递条子，
    # 真动手要 Kira 活着（内存里的启用状态只有它能改）。
    guard_rows = []
    try:
        if do_fix:
            guard_rows = guard_check(cfg, state)
            for g in guard_rows:
                actions.append({"t": time.strftime("%H:%M:%S"),
                                "what": "请 Kira 熔断插件 " + g["pid"],
                                "ok": True, "msg": "%s | %s" % (g["reason"], g.get("sample") or "")})
    except Exception as exc:
        actions.append({"t": time.strftime("%H:%M:%S"), "what": "插件熔断判定失败",
                        "ok": False, "msg": str(exc)[:160]})

    bad = [k for k, v in layers.items() if not v["ok"]]
    verdict = "healthy" if not bad else ("down" if not layers["kira_proc"]["ok"] else "degraded")

    hist = list(state.get("actions") or [])
    hist.extend(actions)
    hist = hist[-int(cfg.get("log_tail") or 200):]

    state.update({
        "at": int(time.time()),
        "at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": verdict,
        "bad": bad,
        "layers": layers,
        "actions": hist,
        "source": source,
        "took_ms": int((time.time() - t0) * 1000),
        "pid": os.getpid(),
        "guard": [{"pid": g["pid"], "reason": g["reason"], "at": g["at"]} for g in guard_rows][-10:],
        "env": runtime_env(),
        "cfg_seen": {"kira_port": kira_port, "adapter_port": ad_port,
                     "cooldown": cfg.get("cooldown"),
                     "max_retry_per_hour": cfg.get("max_retry_per_hour")},
    })
    save_json(state_path(), state)
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="550C watchdog")
    ap.add_argument("--once", action="store_true", help="自检一轮就退出")
    ap.add_argument("--loop", action="store_true", help="常驻轮转")
    ap.add_argument("--status", action="store_true", help="打印上次结果")
    ap.add_argument("--init", action="store_true", help="写一份默认配置")
    ap.add_argument("--dry", action="store_true", help="只查不修")
    ap.add_argument("--interval", type=int, default=0, help="覆盖轮转间隔")
    args = ap.parse_args(argv)

    if args.init:
        p = cfg_path()
        if p.is_file():
            print("配置已存在：%s" % p)
        else:
            save_json(p, DEFAULT_CFG)
            print("已写入默认配置：%s" % p)
        print("下一步：把 restart_adapter / restart_kira 改成你自己的启动命令")
        return 0

    if args.status:
        print(json.dumps(load_state(), ensure_ascii=False, indent=2))
        return 0

    cfg = load_cfg()
    if not cfg.get("enabled", True):
        print("[watchdog] 配置里关掉了")
        return 0

    if args.once:
        st = check_once(cfg, do_fix=not args.dry, source="cli")
        print(json.dumps({k: st[k] for k in ("at_text", "verdict", "bad", "guard", "actions") if k in st},
                         ensure_ascii=False, indent=2))
        return 0 if st.get("verdict") == "healthy" else 1

    interval = args.interval or int(cfg.get("interval") or 120)
    print("[watchdog] 起来，%ss 一轮，状态写 %s" % (interval, state_path()), flush=True)
    while True:
        src = "loop"
        try:
            if trigger_path().is_file():
                trigger_path().unlink()
                src = "panel"
        except Exception:
            pass
        try:
            st = check_once(cfg, do_fix=True, source=src)
            print("[watchdog] %s %s%s" % (st["at_text"], st["verdict"],
                                          (" " + ",".join(st["bad"])) if st.get("bad") else ""),
                  flush=True)
        except Exception as exc:
            print("[watchdog] 本轮炸了：%s" % exc, file=sys.stderr, flush=True)
        time.sleep(max(15, interval))


if __name__ == "__main__":
    raise SystemExit(main())
