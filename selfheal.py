"""550C 自愈：看门狗桥 + 报错自动修理。

两条线，故意分开：

  自检修 —— 桥给独立终端里跑的 scripts/watchdog.py。
            KiraAI 自己不去修自己，它只写一个 trigger 文件，
            谁来修、怎么修，全交给那个不依赖 AI 的独立进程。

  修报错 —— 在 KiraAI 还活着的时候，把日志里那些「修了会好」的报错
            按规则收拾掉：重载插件、清采样、checkpoint、拉起链路。

两个按钮在面板上长一样，但走的完全不是一条路：
前者在 Kira 死了以后还能用（因为真正干活的不在 Kira 里），
后者只在 Kira 活着的时候有意义。混在一起就会变成「AI 修 AI」的死循环。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    from . import repair as repair_rules
except ImportError:
    import repair as repair_rules

PLUGIN_ID = "kira-ai-plugin-550c-boot"

WD_DEFAULTS = {
    "enabled": True,
    "interval": 120,
    "kira_host": "127.0.0.1",
    "kira_port": 8080,
    "adapter_host": "127.0.0.1",
    "adapter_port": 3001,
    "timeout": 3,
    "restart_adapter": "",
    "restart_kira": "",
    "cooldown": 180,
    "max_retry_per_hour": 6,
}

REPAIR_DEFAULTS = {
    "repair_auto": True,          # 后台自动查未修的报错
    "repair_interval_min": 30,    # 多少分钟扫一次
    "repair_auto_max_risk": "low",  # 自动模式只做到这一级（low / mid / none）
    "repair_cooldown_min": 20,    # 同一类报错多久内不重复修
    "watchdog_inline": True,      # 看门狗没在跑时，面板代它跑一次 --once
}


def _data_root() -> Path:
    try:
        from core.utils.path_utils import get_data_path

        return Path(get_data_path())
    except Exception:
        return Path(__file__).resolve().parents[2]


class SelfHealer:
    def __init__(self, host):
        self.host = host                     # Boot550CPlugin，要它的日志与插件管理器
        self.dir = _data_root() / "plugin_data" / PLUGIN_ID
        self._task = None
        self._busy = False
        self._last_scan = 0
        self._last_fix: dict = {}

    # ------------------------------------------------------------ 路径

    def _p(self, name: str) -> Path:
        return self.dir / name

    def wd_state_path(self) -> Path:
        return self._p("watchdog_state.json")

    def wd_cfg_path(self) -> Path:
        return self._p("watchdog.json")

    def trigger_path(self) -> Path:
        return self._p("watchdog_trigger")

    def repair_state_path(self) -> Path:
        return self._p("repair_state.json")

    def script_path(self) -> Path:
        return Path(__file__).parent / "scripts" / "watchdog.py"

    # ------------------------------------------------------------ 配置

    def settings(self) -> dict:
        cfg = dict(REPAIR_DEFAULTS)
        live = getattr(self.host, "plugin_cfg", None) or {}
        for k, v in REPAIR_DEFAULTS.items():
            if k in live and live[k] is not None:
                cfg[k] = live[k]
        try:
            cfg["repair_interval_min"] = max(5, min(720, int(cfg["repair_interval_min"])))
        except Exception:
            cfg["repair_interval_min"] = 30
        try:
            cfg["repair_cooldown_min"] = max(1, int(cfg["repair_cooldown_min"]))
        except Exception:
            cfg["repair_cooldown_min"] = 20
        if cfg.get("repair_auto_max_risk") not in ("low", "mid", "none"):
            cfg["repair_auto_max_risk"] = "low"
        return cfg

    def wd_cfg(self) -> dict:
        cfg = dict(WD_DEFAULTS)
        p = self.wd_cfg_path()
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update({k: v for k, v in data.items() if k in WD_DEFAULTS})
            except Exception:
                pass
        return cfg

    def read_json(self, p: Path) -> dict:
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def write_json(self, p: Path, obj) -> bool:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
            return True
        except Exception as exc:
            print("[550C/selfheal] 写 %s 失败：%s" % (p, exc), file=sys.stderr)
            return False

    # ------------------------------------------------------------ 看门狗

    def watchdog_status(self) -> dict:
        st = self.read_json(self.wd_state_path())
        cfg = self.wd_cfg()
        now = int(time.time())
        at = int(st.get("at") or 0)
        age = (now - at) if at else None
        interval = int(cfg.get("interval") or 120)
        # 三轮没冒泡就当它没在跑。看门狗自己也可能被系统杀掉，这很常见。
        running = bool(at) and age is not None and age <= max(90, interval * 3)
        # 顺带用进程表确认一次，别只信时间戳
        if not running and self._proc_running():
            running = True
        return {
            "running": running,
            "age_sec": age,
            "state": st,
            "cfg": cfg,
            "trigger_pending": self.trigger_path().is_file(),
            "script": str(self.script_path()),
            "script_ok": self.script_path().is_file(),
            "dir": str(self.dir),
        }

    @staticmethod
    def _proc_running() -> bool:
        """别假定有 pgrep：Linux/Termux/macOS 有，Windows 默认没有。"""
        for cmd in (["pgrep", "-f", "watchdog.py"],
                    ["ps", "-A", "-o", "command"],
                    ["ps", "-ef"]):
            try:
                out = subprocess.run(cmd, capture_output=True, timeout=4)
                if "watchdog.py" in (out.stdout or b"").decode("utf-8", "ignore"):
                    return True
            except Exception:
                continue
        if os.name == "nt":
            try:
                out = subprocess.run(["wmic", "process", "get", "CommandLine"],
                                     capture_output=True, timeout=6)
                if b"watchdog.py" in (out.stdout or b""):
                    return True
            except Exception:
                pass
        try:
            import psutil
            for pr in psutil.process_iter(["cmdline"]):
                if any("watchdog.py" in str(x) for x in (pr.info.get("cmdline") or [])):
                    return True
        except Exception:
            pass
        return False

    def daemon_log(self) -> Path:
        return self._p("watchdog_daemon.log")

    def spawn_daemon(self) -> dict:
        """把常驻看门狗拉起来（独立会话，输出进日志，不跟 Kira 生死绑定）。"""
        if self._proc_running():
            return {"ok": True, "running": True, "msg": "常驻进程已在运行"}
        script = self.script_path()
        if not script.is_file():
            return {"ok": False, "running": False, "msg": "脚本不存在：%s" % script}
        ensure = int(self.wd_cfg().get("interval") or 120)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            log = self.daemon_log()
            with open(log, "ab") as fh:
                fh.write(("\n[%s] 由 KiraAI 拉起常驻看门狗\n"
                          % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
                fh.flush()
                subprocess.Popen(
                    [sys.executable, str(script), "--loop", "--interval", str(ensure)],
                    cwd=str(script.parent),
                    stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True,
                )
            for _ in range(10):
                time.sleep(0.3)
                if self._proc_running():
                    return {"ok": True, "running": True, "msg": "常驻进程已启动"}
            return {"ok": True, "running": bool(self._proc_running()),
                    "msg": "已发起启动，进程尚未就绪"}
        except Exception as exc:
            return {"ok": False, "running": False, "msg": "启动失败：%s" % exc}

    async def ensure_daemon(self) -> dict:
        res = await asyncio.to_thread(self.spawn_daemon)
        if res.get("ok") and res.get("running"):
            print("[550C/selfheal] 看门狗：%s" % res.get("msg"), file=sys.stderr)
        elif not res.get("ok"):
            print("[550C/selfheal] 看门狗没拉起来：%s" % res.get("msg"), file=sys.stderr)
        return res

    async def run_watchdog(self, reason: str = "panel") -> dict:
        """自检修：优先让常驻看门狗去干，它不在就自己代跑一次。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        cfg = self.settings()
        # 常驻进程不在就先把它拉起来，按钮点下去要真的有人干活
        if not self._proc_running():
            spawned = await self.ensure_daemon()
            if not (spawned.get("ok") and spawned.get("running")):
                return {"ok": False, "mode": "none",
                        "msg": "常驻进程未能启动：%s" % spawned.get("msg")}
        wrote = self.write_json(self.trigger_path(), {"at": int(time.time()), "why": reason})
        if self._proc_running() or not cfg.get("watchdog_inline", True):
            return {"ok": bool(wrote), "mode": "trigger",
                    "msg": "已通知常驻看门狗，本轮巡检即将执行"
                           if wrote else "触发文件写入失败，请检查目录权限"}

        script = self.script_path()
        if not script.is_file():
            return {"ok": False, "mode": "none", "msg": "看门狗脚本不在：%s" % script}
        try:
            res = await asyncio.to_thread(
                lambda: subprocess.run([sys.executable, str(script), "--once"],
                                       capture_output=True, timeout=120)
            )
            text = (res.stdout + res.stderr).decode("utf-8", "ignore").strip()
            return {"ok": res.returncode in (0, 1), "mode": "inline",
                    "msg": (text.splitlines()[-6:] and "\n".join(text.splitlines()[-6:])) or "跑完了",
                    "state": self.read_json(self.wd_state_path())}
        except subprocess.TimeoutExpired:
            return {"ok": False, "mode": "inline", "msg": "看门狗跑超时了，先别急着重启主进程"}
        except Exception as exc:
            return {"ok": False, "mode": "inline", "msg": "代跑失败：%s" % exc}

    # ------------------------------------------------------------ 报错修理

    async def scan(self, limit: int = 120) -> dict:
        data = await asyncio.to_thread(self.host._errors_today, limit)
        p = repair_rules.plan(data.get("groups") or [])
        p["date"] = data.get("date")
        p["counts"] = data.get("counts")
        p["ok"] = data.get("ok", True)
        p["msg"] = data.get("msg")
        return p

    def _repair_log(self) -> list:
        p = self._p("repairs.jsonl")
        if not p.is_file():
            return []
        out = []
        try:
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines()[-300:]:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
        except Exception:
            return []
        return out

    def _append_repair(self, row: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self._p("repairs.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            print("[550C/selfheal] 记修复日志失败：%s" % exc, file=sys.stderr)

    def _cooling(self, key: str, minutes: int) -> bool:
        last = float(self._last_fix.get(key) or 0)
        return (time.time() - last) < minutes * 60

    async def execute(self, fix: str, text: str = "", src: str = "") -> dict:
        """真的动手。只用 KiraAI 活着时才拿得到的东西，出事一律吞。"""
        try:
            if fix in ("wd_adapter", "wd_kira"):
                r = await self.run_watchdog("repair:" + fix)
                return {"ok": r.get("ok"), "msg": r.get("msg")}

            if fix == "reload_plugin":
                pid = self._guess_plugin(text)
                if not pid:
                    return {"ok": False, "msg": "报错里认不出是哪个插件，不敢乱重载"}
                mgr = getattr(self.host.ctx, "plugin_mgr", None)
                if mgr is None or not hasattr(mgr, "reload"):
                    return {"ok": False, "msg": "当前环境拿不到插件管理器"}
                try:
                    await mgr.prepare_plugin_reload(pid)
                except Exception:
                    pass
                await mgr.reload(pid)
                return {"ok": True, "msg": "已重载 %s" % pid}

            if fix == "trim_samples":
                p = Path(__file__).parent / "samples.jsonl"
                if not p.is_file():
                    return {"ok": True, "msg": "采样文件本来就没有"}
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                keep = lines[-4000:]
                p.write_text("\n".join(keep) + "\n", encoding="utf-8")
                return {"ok": True, "msg": "采样从 %d 行截到 %d 行" % (len(lines), len(keep))}

            if fix == "db_checkpoint":
                db = _data_root() / "data.db"
                if not db.is_file():
                    return {"ok": False, "msg": "没找到 data.db"}
                import sqlite3

                def _ck():
                    conn = sqlite3.connect(str(db), timeout=10)
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        conn.commit()
                    finally:
                        conn.close()

                await asyncio.to_thread(_ck)
                return {"ok": True, "msg": "已做 WAL checkpoint"}

            if fix == "clear_cache":
                root = _data_root()
                cutoff = time.time() - 3 * 86400
                freed = 0
                for sub in ("plugins_media_cache", "temp"):
                    d = root / sub
                    if not d.is_dir():
                        continue
                    for f in d.rglob("*"):
                        try:
                            if f.is_file() and f.stat().st_mtime < cutoff:
                                freed += f.stat().st_size
                                f.unlink()
                        except Exception:
                            continue
                return {"ok": True, "msg": "清了 %.1f MB 老缓存" % (freed / (1024 ** 2))}

            return {"ok": False, "msg": "这一类规则没有对应动作"}
        except Exception as exc:
            return {"ok": False, "msg": "修的时候炸了：%s" % exc}

    def _guess_plugin(self, text: str) -> str:
        text = text or ""
        mgr = getattr(self.host.ctx, "plugin_mgr", None)
        try:
            ids = list((getattr(mgr, "plugins", {}) or {}).keys())
        except Exception:
            ids = []
        for pid in ids:
            if pid and pid in text:
                return pid
        m = re.search(r"([A-Za-z0-9_\-]{4,60}plugin[A-Za-z0-9_\-]*)", text, re.I)
        return m.group(1) if m else ""

    async def run_repairs(self, plan: dict = None, mode: str = "manual") -> dict:
        if self._busy:
            return {"ok": False, "msg": "正在修，别叠着来"}
        self._busy = True
        cfg = self.settings()
        max_risk = "mid" if mode == "manual" else cfg.get("repair_auto_max_risk", "low")
        order = {"low": 0, "mid": 1, "none": 2}
        lim = order.get(max_risk, 0)
        cooldown = int(cfg.get("repair_cooldown_min") or 20)
        try:
            plan = plan or await self.scan()
            done, skipped, seen = [], [], set()
            for row in plan.get("rows") or []:
                fix = row.get("fix") or "none"
                if fix == "none":
                    continue
                if order.get(row.get("risk"), 2) > lim:
                    skipped.append({"name": row.get("name"), "msg": "风险高于本次允许的级别"})
                    continue
                if fix in seen:          # 同一类动作一轮只做一次，别连着重启五遍
                    skipped.append({"name": row.get("name"), "msg": "同类动作这轮已经做过"})
                    continue
                if self._cooling(fix, cooldown):
                    skipped.append({"name": row.get("name"), "msg": "冷却中，刚才才修过"})
                    continue
                seen.add(fix)
                res = await self.execute(fix, row.get("head") or "", row.get("src") or "")
                self._last_fix[fix] = time.time()
                row = dict(row)
                row["result"] = res
                row["mode"] = mode
                row["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                done.append(row)
                self._append_repair(row)
            return {"ok": True, "mode": mode, "done": done, "skipped": skipped,
                    "msg": ("修了 %d 类" % len(done)) if done else "这一轮没有能下手的地方",
                    "plan": plan}
        finally:
            self._busy = False
            self._last_scan = time.time()

    # ------------------------------------------------------------ 后台

    def status(self) -> dict:
        cfg = self.settings()
        state = self.read_json(self.repair_state_path())
        return {
            "settings": cfg,
            "busy": self._busy,
            "running": bool(self._task and not self._task.done()),
            "last_run": state.get("last_run"),
            "last_mode": state.get("last_mode"),
            "last_summary": state.get("last_summary"),
            "last_plan": state.get("last_plan"),
            "history": self._repair_log()[-40:],
            "watchdog": self.watchdog_status(),
        }

    async def _loop(self) -> None:
        # 启动先等一会儿，别和插件初始化抢资源
        await asyncio.sleep(12)
        try:
            # 一上来先把常驻看门狗补上：手机重启 → Kira 起来 → 它跟着起来，不用手敲
            await self.ensure_daemon()
        except Exception as exc:
            print("[550C/selfheal] 补看门狗失败：%s" % exc, file=sys.stderr)
        await asyncio.sleep(33)
        while True:
            try:
                cfg = self.settings()
                # 每轮顺带确认常驻进程还在，掉了就重新拉起
                try:
                    if not self._proc_running():
                        await self.ensure_daemon()
                except Exception:
                    pass
                if not cfg.get("repair_auto", True):
                    await asyncio.sleep(300)
                    continue
                plan = await self.scan()
                if plan.get("fixable"):
                    res = await self.run_repairs(plan, mode="auto")
                    summary = res.get("msg")
                else:
                    summary = "没有可修的报错"
                self.write_json(self.repair_state_path(), {
                    "last_run": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_mode": "auto",
                    "last_summary": summary,
                    "last_plan": {k: plan.get(k) for k in
                                  ("fixable", "auto", "manual", "unknown", "date", "counts")},
                })
                await asyncio.sleep(max(300, int(cfg.get("repair_interval_min") or 30) * 60))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("[550C/selfheal] 自愈循环出错：%s" % exc, file=sys.stderr)
                await asyncio.sleep(180)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="550c-selfheal")

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
