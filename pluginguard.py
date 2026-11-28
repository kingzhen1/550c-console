"""550C 插件熔断：某个已装插件把聊天搞坏了，就先停它，换聊天活着。

和 updater 完全是两件事：
    updater 管「版本对不对」；
    这里管「还能不能用」。

判定只看日志：同一个插件在时间窗里持续往外吐 ERROR，
最先被牺牲的应该是它，而不是整个 KiraAI。
只要还能对话，人就还有手去把它修回来；对话断了，什么都修不了。

三条硬规矩：
  1. 只停「疑似影响使用」的插件，够不到阈值绝不碰；
  2. 自己、白名单、core 内置一律不停；
  3. 停之前记一笔（circuit.json），停之后进冷却，绝不自己反复开关。

两层触发，谁先看见算谁的：
  · Kira 内部循环：guard_interval_sec 一轮，读日志；
  · 独立跑在终端里的那个看门狗：它读同一份日志，把请求写进 guard_requests.json，
    Kira 下一轮读到就执行 —— 于是插件熔断也是全天候的。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

PLUGIN_ID = "kira-ai-plugin-550c-boot"

GUARD_DEFAULTS = {
    "guard_auto": True,          # 自动熔断总开关
    "guard_interval_sec": 60,    # 多久看一次日志
    "guard_window_min": 20,      # 只看最近多少分钟
    "guard_min_errors": 3,       # 窗口内至少几条 ERROR 才考虑动手
    "guard_cooldown_min": 60,    # 刚停过的插件，多久内不再重复处理
    "guard_restore_hours": 24,   # 停满多少小时后自动放回来试一次（0=不自动）
    "guard_max_trips_per_day": 3,  # 同一天最多停同一个插件几次，超了就等人
    "guard_whitelist": [
        PLUGIN_ID,
        "default-chat",
        "default-chat（z）",
        "kira_session_plugin",
        "kira-ai-plugin-payload-defense",
        "kira_persona_progression",
        "kira_accelerator",
        "sustained-chat",
    ],
}

_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(ERROR|CRITICAL|WARNING)\s+\[([^\]]*)\]\s?(.*)$"
)

# 「影响使用」的强信号：插件在真正干活的时候抛错
STRONG = re.compile(
    r"(?i)traceback|exception|timeout|超时|提取|extract|崩溃|crash|"
    r"failed to call tool|tool_result: error|keyerror|attributeerror|typeerror|importerror"
)

# 加载失败不算运行期故障：它本来就没参与工作，停它没意义
LOADFAIL = re.compile(r"(?i)failed to (initialize|load) plugin|加载插件失败|no module named")


def _host_logger():
    try:
        from core.plugin import logger

        return logger
    except Exception:
        return None


class PluginGuard:
    def __init__(self, host):
        self.host = host                       # Boot550CPlugin，拿它的日志与插件管理器
        self.dir = Path(__file__).resolve()
        try:
            from .selfheal import _data_root

            self.dir = _data_root() / "plugin_data" / PLUGIN_ID
        except Exception:
            self.dir = Path(__file__).resolve().parents[2] / "plugin_data" / PLUGIN_ID
        self._task = None
        self._busy = False
        self._last_tick = 0

    # ------------------------------------------------------------ 路径

    def _p(self, name: str) -> Path:
        return self.dir / name

    def circuit_path(self) -> Path:
        return self._p("circuit.json")

    def request_path(self) -> Path:
        return self._p("guard_requests.json")

    def state_path(self) -> Path:
        return self._p("guard_state.json")

    # ------------------------------------------------------------ 读写

    def read_json(self, p: Path, default=None):
        if not p.is_file():
            return {} if default is None else default
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else ({} if default is None else default)
        except Exception:
            return {} if default is None else default

    def write_json(self, p: Path, obj) -> None:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception as exc:
            lg = _host_logger()
            if lg:
                lg.warning("[550C/guard] 写 %s 失败：%s", p.name, exc)

    # ------------------------------------------------------------ 配置

    def settings(self) -> dict:
        cfg = dict(GUARD_DEFAULTS)
        live = getattr(self.host, "plugin_cfg", None) or {}
        for k, v in GUARD_DEFAULTS.items():
            if k in live and live[k] is not None:
                cfg[k] = live[k]
        try:
            cfg["guard_interval_sec"] = max(20, min(600, int(cfg["guard_interval_sec"])))
        except Exception:
            cfg["guard_interval_sec"] = 60
        try:
            cfg["guard_window_min"] = max(3, min(180, int(cfg["guard_window_min"])))
        except Exception:
            cfg["guard_window_min"] = 20
        try:
            cfg["guard_min_errors"] = max(2, min(50, int(cfg["guard_min_errors"])))
        except Exception:
            cfg["guard_min_errors"] = 3
        wl = cfg.get("guard_whitelist")
        if isinstance(wl, str):
            wl = [x.strip() for x in wl.replace("，", ",").split(",") if x.strip()]
        if not isinstance(wl, list):
            wl = list(GUARD_DEFAULTS["guard_whitelist"])
        cfg["guard_whitelist"] = [str(x) for x in wl]
        return cfg

    def whitelist(self) -> set:
        wl = set(self.settings().get("guard_whitelist") or [])
        wl.add(PLUGIN_ID)
        return wl

    # ------------------------------------------------------------ 认得谁是谁

    def known_plugins(self) -> dict:
        """插件 id → 别名集合（id、目录名、去掉 kira-ai-plugin- 前缀的名字）。"""
        mgr = getattr(getattr(self.host, "ctx", None), "plugin_mgr", None)
        ids = set()
        for attr in ("plugin_enabled", "plugins", "plugin_instances"):
            try:
                val = getattr(mgr, attr, None)
                if isinstance(val, dict):
                    ids |= {str(k) for k in val.keys()}
            except Exception:
                continue
        try:
            pj = Path(__file__).resolve().parents[2] / "config" / "plugins.json"
            if pj.is_file():
                ids |= {str(k) for k in json.loads(pj.read_text(encoding="utf-8")).keys()}
        except Exception:
            pass
        try:
            root = Path(__file__).resolve().parents[1]
            for d in root.iterdir():
                if d.is_dir() and not d.name.startswith((".", "__")):
                    ids.add(d.name)
        except Exception:
            pass
        alias = {}
        for pid in ids:
            if not pid or pid == PLUGIN_ID:
                continue
            names = {pid, pid.lower()}
            short = re.sub(r"(?i)^kira[-_]ai[-_]plugin[-_]", "", pid)
            names.add(short)
            names.add(short.lower())
            alias[pid] = {n for n in names if len(n) >= 4}
        return alias

    def _match_plugin(self, text: str, alias: dict) -> str:
        """认插件要靠边界，不能拿一个短词去到处碰，误伤比漏判难收拾。"""
        text = text or ""
        best, best_len = "", 0
        for pid, names in alias.items():
            for n in names:
                if len(n) < 6:
                    continue
                pat = r"(?<![A-Za-z0-9_\-])" + re.escape(n) + r"(?![A-Za-z0-9_\-])"
                if re.search(pat, text, re.I) and len(n) > best_len:
                    best, best_len = pid, len(n)
        return best

    # ------------------------------------------------------------ 读日志

    def scan(self, limit_bytes: int = 1024 * 1024) -> dict:
        """把最近窗口内的插件级 ERROR 归到各自的插件名下。"""
        cfg = self.settings()
        window = int(cfg["guard_window_min"]) * 60
        path = None
        try:
            path = self.host._log_file()
        except Exception:
            path = None
        if path is None or not Path(path).is_file():
            for cand in (self.dir.parents[1] / "log.log", self.dir.parents[1] / "logs" / "log.log"):
                try:
                    if cand.is_file():
                        path = cand
                        break
                except Exception:
                    continue
        if path is None or not Path(path).is_file():
            return {"ok": False, "msg": "拿不到日志文件", "rows": {}, "counts": {}}
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            return {"ok": False, "msg": "读日志失败：%s" % exc, "rows": {}, "counts": {}}
        lines = raw.splitlines()[-15000:]

        alias = self.known_plugins()
        now = time.time()
        rows: dict = {}
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
            pid = self._match_plugin(text, alias)
            if not pid:
                continue
            row = rows.setdefault(pid, {"pid": pid, "err": 0, "warn": 0, "strong": 0,
                                        "loadfail": 0, "sample": "", "first": date + " " + hhmmss,
                                        "last": date + " " + hhmmss})
            row["last"] = date + " " + hhmmss
            if LOADFAIL.search(text):
                row["loadfail"] += 1
                continue
            if lv in ("ERROR", "CRITICAL"):
                row["err"] += 1
                if STRONG.search(text):
                    row["strong"] += 1
                    if not row["sample"]:
                        row["sample"] = (src + " " + msg)[:180]
            else:
                row["warn"] += 1
        for row in rows.values():
            # 运行期抛错：达阈值就算热；反复起不来也算，它会把启动拖成拉锯战
            row["hot"] = bool(
                row["err"] >= int(cfg["guard_min_errors"])
                or row["strong"] >= 2
                or row["loadfail"] >= max(4, int(cfg["guard_min_errors"]) * 2)
            )
            if row["loadfail"] and not row["err"]:
                row["sample"] = row["sample"] or "反复初始化失败"
        return {"ok": True, "window_min": cfg["guard_window_min"],
                "rows": rows, "at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ------------------------------------------------------------ 熔断

    def circuits(self) -> dict:
        return self.read_json(self.circuit_path(), {}).get("items", {}) or {}

    def _save_circuits(self, items: dict) -> None:
        self.write_json(self.circuit_path(), {"items": items,
                                              "at": time.strftime("%Y-%m-%d %H:%M:%S")})

    async def _disable(self, pid: str, reason: str, errors: int, sample: str, auto: bool = True) -> dict:
        mgr = getattr(getattr(self.host, "ctx", None), "plugin_mgr", None)
        if mgr is None or not hasattr(mgr, "set_plugin_enabled"):
            return {"ok": False, "msg": "拿不到插件管理器"}
        try:
            if not mgr.is_plugin_enabled(pid):
                return {"ok": True, "msg": "本来就没启用"}
            await mgr.set_plugin_enabled(pid, False)
        except Exception as exc:
            return {"ok": False, "msg": "停用失败：%s" % exc}

        items = self.circuits()
        today = time.strftime("%Y-%m-%d")
        prev = items.get(pid) or {}
        trips = int(prev.get("trips") or 0)
        if prev.get("day") == today:
            trips += 1
        else:
            trips = 1
        items[pid] = {
            "pid": pid, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "day": today,
            "trips": trips, "auto": bool(auto), "reason": reason,
            "errors": int(errors), "sample": sample,
            "restore_at": int(time.time() + float(self.settings().get("guard_restore_hours") or 0) * 3600),
            "needs_human": trips >= int(self.settings().get("guard_max_trips_per_day") or 3),
        }
        self._save_circuits(items)
        lg = _host_logger()
        if lg:
            lg.warning("[550C/guard] 熔断插件 %s：%s（%d 条报错）", pid, reason, errors)
        return {"ok": True, "msg": "已停用 %s" % pid}

    async def restore(self, pid: str) -> dict:
        mgr = getattr(getattr(self.host, "ctx", None), "plugin_mgr", None)
        if mgr is None:
            return {"ok": False, "msg": "拿不到插件管理器"}
        try:
            await mgr.set_plugin_enabled(pid, True)
        except Exception as exc:
            return {"ok": False, "msg": "恢复失败：%s" % exc}
        items = self.circuits()
        items.pop(pid, None)
        self._save_circuits(items)
        return {"ok": True, "msg": "已放回 %s" % pid}

    # ------------------------------------------------------------ 一轮

    def _take_requests(self) -> list:
        """看门狗（独立进程）写进来的熔断请求。"""
        p = self.request_path()
        if not p.is_file():
            return []
        data = self.read_json(p, {})
        try:
            p.unlink()
        except Exception:
            pass
        reqs = data.get("items")
        return reqs if isinstance(reqs, list) else []

    async def tick(self, source: str = "loop") -> dict:
        if self._busy:
            return {"ok": False, "msg": "上一轮还没走完"}
        cfg = self.settings()
        self._busy = True
        done, skipped = [], []
        try:
            # 外部请求优先：看门狗看日志看得比面板早
            for req in self._take_requests():
                pid = str((req or {}).get("pid") or "")
                if not pid:
                    continue
                if pid in self.whitelist():
                    skipped.append({"pid": pid, "msg": "白名单，不停"})
                    continue
                cur = self.circuits().get(pid) or {}
                if cur.get("needs_human"):
                    skipped.append({"pid": pid, "msg": "今天停太多次了，等人看一眼"})
                    continue
                res = await self._disable(pid, str((req or {}).get("reason") or "看门狗判定异常"),
                                          int((req or {}).get("errors") or 0),
                                          str((req or {}).get("sample") or ""))
                done.append({"pid": pid, "src": "watchdog", **res})

            # 到点放回来的
            items = self.circuits()
            if float(cfg.get("guard_restore_hours") or 0) > 0:
                for pid, row in list(items.items()):
                    if row.get("needs_human"):
                        continue
                    if int(row.get("restore_at") or 0) and time.time() >= int(row["restore_at"]):
                        res = await self.restore(pid)
                        done.append({"pid": pid, "src": "auto-restore", **res})

            # 自己看一遍日志
            if not cfg.get("guard_auto", True):
                return {"ok": True, "msg": "自动熔断关着", "done": done, "skipped": skipped}
            plan = await self._scan_async()
            wl = self.whitelist()
            cooling = int(cfg.get("guard_cooldown_min") or 60) * 60
            old_state = self.read_json(self.state_path(), {})
            baseline = old_state.get("baseline") or {}
            rows = plan.get("rows") or {}

            # 只看「这一轮新冒出来」的报错：启动时那批配置噪音要算，但不算它头上
            cands = []
            for pid, row in rows.items():
                if not row.get("hot"):
                    continue
                prev = baseline.get(pid) or {}
                grew = row["err"] - int(prev.get("err") or 0)
                same_batch = (prev.get("last") == row["last"] and grew <= 0)
                if pid not in baseline:
                    # 头一次见：光是条数多不算，得带着堆栈/超时这种硬信号，
                    # 或者条数已经多到不像话，才值得动它
                    if row.get("strong", 0) < 2 and row["err"] < int(cfg.get("guard_min_errors") or 3) * 2:
                        continue
                    grew = row["err"]
                elif same_batch:
                    continue
                cands.append((row["err"] + row.get("strong", 0), pid, row, grew))

            cands.sort(reverse=True)
            budget = 2   # 一轮最多停两个：停多了等于换个方式把聊天搞坏
            for _score, pid, row, grew in cands:
                if budget <= 0:
                    skipped.append({"pid": pid, "msg": "这一轮动手次数到上限"})
                    continue
                if pid in wl:
                    skipped.append({"pid": pid, "msg": "白名单，不停"})
                    continue
                cur = self.circuits().get(pid) or {}
                if cur.get("needs_human"):
                    skipped.append({"pid": pid, "msg": "今天停太多次了，等人看一眼"})
                    continue
                try:
                    last = time.mktime(time.strptime(cur.get("at"), "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    last = 0
                if last and time.time() - last < cooling:
                    skipped.append({"pid": pid, "msg": "冷却中"})
                    continue
                reason = "窗口内 %d 条 ERROR（新增 %d，强信号 %d）" % (
                    row["err"], max(grew, 0), row.get("strong", 0))
                res = await self._disable(pid, reason, row["err"], row.get("sample") or "")
                if res.get("ok"):
                    budget -= 1
                done.append({"pid": pid, "src": source, **res})

            self.write_json(self.state_path(), {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "done": done[-20:], "skipped": skipped[-20:],
                "baseline": {k: {"err": v.get("err"), "warn": v.get("warn"),
                                 "loadfail": v.get("loadfail"), "last": v.get("last"),
                                 "strong": v.get("strong")}
                             for k, v in (plan.get("rows") or {}).items()},
                "plan": {k: {kk: vv for kk, vv in v.items() if kk in
                             ("err", "warn", "strong", "hot", "sample")}
                         for k, v in (plan.get("rows") or {}).items()},
            })
            return {"ok": True, "done": done, "skipped": skipped, "plan": plan}
        finally:
            self._busy = False
            self._last_tick = time.time()

    async def _scan_async(self):
        import asyncio

        return await asyncio.to_thread(self.scan)

    def status(self) -> dict:
        return {
            "settings": self.settings(),
            "circuits": self.circuits(),
            "last": self.read_json(self.state_path(), {}),
            "running": bool(self._task and not self._task.done()),
            "whitelist": sorted(self.whitelist()),
        }

    # ------------------------------------------------------------ 常驻

    async def start(self) -> None:
        import asyncio

        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="550c-plugin-guard")

    async def stop(self) -> None:
        import asyncio

        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def _loop(self) -> None:
        import asyncio

        await asyncio.sleep(20)   # 别跟插件初始化抢
        while True:
            try:
                cfg = self.settings()
                await self.tick(source="loop")
                await asyncio.sleep(float(cfg.get("guard_interval_sec") or 60))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lg = _host_logger()
                if lg:
                    lg.warning("[550C/guard] 循环出错：%s", exc)
                await asyncio.sleep(120)
