"""550C 插件自动更新器。

单独一个模块，只管「定时看一眼市场 → 已装的谁有新版本 → 换掉 → 记录」，
不碰面板主逻辑，也不给任何人发消息。想关就在插件配置里把总开关关掉。

几条自己给自己定的规矩：

* 绝不更新自己（550C）和 kira_ops。更新循环把自己换掉，循环就断在半路。
* 绝不装新插件，只更新「已经装在本地、市场里也有收录」的那几个。
* 每次检查最多换 N 个、每个之间歇几秒，别一口气把整台机器掀翻。
* 换之前先把旧目录整份备份；换完装不上、或者插件身份对不上，就原地回滚。
* 同一个插件连续失败两次就拉进黑名单，不再反复折腾，等人自己去看。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from pathlib import Path

from core.plugin import logger

PLUGIN_ID = "kira-ai-plugin-550c-boot"

# 谁都不能动：自己，以及做备份/回滚的那位
PROTECTED = (PLUGIN_ID, "kira_ops")

# 市场缓存一小时内的算新鲜，过期才去网上拉
MARKET_TTL = 3600
# 主循环的心跳。真正什么时候更新看 next_at，心跳只负责「有没有到点」
LOOP_TICK = 60
# 两个插件之间歇几秒，给插件管理器和磁盘一点喘息
GAP_SECONDS = 6
MAX_HISTORY = 30
# 连续失败到这个数就拉黑
FAIL_LIMIT = 2
# 每个插件留几份旧备份
KEEP_BACKUPS = 3

FALLBACK_STORE_URL = "https://plugins.kira-ai.top/api/plugins/all"

DEFAULTS = {
    "auto_update": True,
    "update_interval_hours": 6,
    "update_max_per_run": 3,
    "update_exclude": "",
}


def _data_root() -> Path:
    try:
        from core.utils.path_utils import get_data_path

        return Path(get_data_path())
    except Exception:
        return Path(__file__).resolve().parents[2]


def _work_dir() -> Path:
    d = _data_root() / "plugin_autoupdate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _newer(latest: str, local: str) -> bool:
    """市场版本是不是真的比本地新。解析不了就退回字符串比较。"""
    a = str(latest or "").strip().lstrip("vV")
    b = str(local or "").strip().lstrip("vV")
    if not a:
        return False
    if not b or b == "-":
        return True
    try:
        from packaging.version import Version

        return Version(a) > Version(b)
    except Exception:
        return a != b


def _fmt_ts(ts) -> str:
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


class AutoUpdater:
    """挂一个后台协程，到点就替哥哥把插件换到最新版。"""

    def __init__(self, host):
        # host 就是 550C 的插件实例，用它拿 plugin_mgr 和配置
        self.host = host
        self._task = None
        self._busy = False
        self._market = {}
        self._market_at = 0.0
        self._market_note = ""
        self._state = self._load_state()

    # ---------------- 配置 ----------------
    def _cfg(self) -> dict:
        cfg = getattr(self.host, "plugin_cfg", None) or {}

        def _int(key: str, lo: int, hi: int) -> int:
            try:
                val = int(cfg.get(key, DEFAULTS[key]))
            except (TypeError, ValueError):
                val = int(DEFAULTS[key])
            return max(lo, min(hi, val))

        exclude = str(cfg.get("update_exclude") or "").strip()
        return {
            "enabled": bool(cfg.get("auto_update", DEFAULTS["auto_update"])),
            "interval_h": _int("update_interval_hours", 1, 168),
            "max_per_run": _int("update_max_per_run", 1, 10),
            "exclude": [x.strip() for x in exclude.replace("，", ",").split(",") if x.strip()],
        }

    def _protected(self) -> list:
        return sorted(set(PROTECTED) | set(self._cfg()["exclude"]))

    # ---------------- 状态持久化 ----------------
    def _state_path(self) -> Path:
        return _work_dir() / "state.json"

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        rows = data.get("history")
        return {
            "last_check": int(data.get("last_check") or 0),
            "next_at": int(data.get("next_at") or 0),
            "last_result": str(data.get("last_result") or ""),
            "last_error": str(data.get("last_error") or ""),
            "history": rows if isinstance(rows, list) else [],
            "blocked": data.get("blocked") if isinstance(data.get("blocked"), dict) else {},
        }

    def _save_state(self) -> None:
        try:
            self._state_path().write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("[550C] 自动更新状态写不下去：%s", exc)

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        cfg = self._cfg()
        now = int(time.time())
        # 刚开机不立刻抢网：先等一个周期，把开机那阵子的加载让出来
        if not self._state.get("next_at") or self._state["next_at"] < now - cfg["interval_h"] * 3600:
            self._state["next_at"] = now + cfg["interval_h"] * 3600
            self._save_state()
        self._task = asyncio.create_task(self._loop(), name="550c-autoupdate")
        logger.info(
            "[550C] 自动更新已挂上：%s，每 %d 小时一次，下次 %s",
            "开启" if cfg["enabled"] else "关闭",
            cfg["interval_h"],
            _fmt_ts(self._state.get("next_at")),
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        self._save_state()

    async def _loop(self) -> None:
        while True:
            try:
                cfg = self._cfg()
                now = time.time()
                next_at = float(self._state.get("next_at") or 0)
                if not cfg["enabled"]:
                    await asyncio.sleep(LOOP_TICK)
                    continue
                if now >= next_at:
                    await self.run_once(manual=False)
                    continue
                wait = min(LOOP_TICK, max(5.0, next_at - now))
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 循环本身不能死
                logger.error("[550C] 自动更新循环抛错：%s", exc)
                await asyncio.sleep(LOOP_TICK)

    # ---------------- 市场 ----------------
    def _disk_market(self):
        """读 data/plugin_src 下最新的那份市场缓存。"""
        out = {}
        newest = 0.0
        try:
            src = _data_root() / "plugin_src"
            files = sorted(src.glob("plugins_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        except Exception:
            return out, newest
        if not files:
            return out, newest
        newest = files[0].stat().st_mtime
        for f in files[:2]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            items = data.get("plugins") if isinstance(data, dict) else None
            if isinstance(items, dict):
                pairs = list(items.items())
            elif isinstance(items, list):
                pairs = [(i.get("plugin_id"), i) for i in items if isinstance(i, dict)]
            else:
                pairs = []
            for pid, item in pairs:
                if not pid or not isinstance(item, dict) or str(pid) in out:
                    continue
                ver = str(item.get("version") or "").strip()
                if not ver:
                    continue
                out[str(pid)] = {
                    "version": ver,
                    "repo": str(item.get("repo") or item.get("repository") or ""),
                }
        return out, newest

    def _store_url(self) -> str:
        """问数据库要当前市场源；读不到就用官方源。"""
        try:
            db = _data_root() / "data.db"
            con = sqlite3.connect(str(db), timeout=3)
            try:
                row = con.execute(
                    "select url from plugin_store_sources where is_current = 1 limit 1"
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
        return FALLBACK_STORE_URL

    @staticmethod
    def _http_json(url: str) -> dict:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "550C-autoupdate"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}

    async def market(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._market and now - self._market_at < MARKET_TTL:
            return self._market

        disk, disk_at = self._disk_market()
        fresh = bool(disk) and (now - disk_at < MARKET_TTL)
        merged = dict(disk)
        note = "本地缓存 %s" % _fmt_ts(disk_at)

        if force or not fresh:
            try:
                data = await asyncio.to_thread(self._http_json, self._store_url())
                items = data.get("plugins") if isinstance(data, dict) else None
                pairs = []
                if isinstance(items, dict):
                    pairs = list(items.items())
                elif isinstance(items, list):
                    pairs = [(i.get("plugin_id"), i) for i in items if isinstance(i, dict)]
                got = {}
                for pid, item in pairs:
                    if not pid or not isinstance(item, dict):
                        continue
                    ver = str(item.get("version") or "").strip()
                    if not ver:
                        continue
                    got[str(pid)] = {
                        "version": ver,
                        "repo": str(item.get("repo") or item.get("repository") or ""),
                    }
                if got:
                    merged = got
                    note = "在线拉取 %s" % _fmt_ts(now)
                elif not merged:
                    note = "市场拉不到，且本地没有缓存"
            except Exception as exc:
                # 网络抖一下不等于「没有新版」：继续用旧缓存判断
                note = "在线拉取失败（%s），沿用 %s" % (type(exc).__name__, _fmt_ts(disk_at))
                logger.warning("[550C] 拉市场失败：%s", exc)

        self._market, self._market_at, self._market_note = merged, now, note
        return merged

    # ---------------- 候选 ----------------
    def plans(self, market=None) -> list:
        """谁该更新。只认本地已经装上的，绝不新装。"""
        mgr = getattr(self.host.ctx, "plugin_mgr", None)
        if mgr is None:
            return []
        market = market or {}
        blocked = self._state.get("blocked") or {}
        protect = set(self._protected())
        out = []
        try:
            infos = list(mgr.list_plugins())
        except Exception as exc:
            logger.warning("[550C] 读插件清单失败：%s", exc)
            return []

        for info in infos:
            pid = str(getattr(info, "plugin_id", "") or "")
            if not pid or pid in protect:
                continue
            if getattr(info, "builtin", False) or getattr(info, "hidden", False):
                continue
            if int(blocked.get(pid) or 0) >= FAIL_LIMIT:
                continue
            item = market.get(pid)
            if not item or not item.get("repo"):
                continue
            local = str(getattr(info, "version", "") or "")
            if not _newer(item["version"], local):
                continue
            out.append({
                "id": pid,
                "name": str(getattr(info, "display_name", "") or pid),
                "from": local or "-",
                "to": item["version"],
                "repo": item["repo"],
            })
        out.sort(key=lambda r: r["id"])
        return out

    # ---------------- 备份 / 回滚 ----------------
    def _backup_root(self) -> Path:
        d = _work_dir() / "backup"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _backup(self, pid: str, dest: Path):
        if not dest or not Path(dest).is_dir():
            return None
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in pid)
        root = self._backup_root() / safe
        root.mkdir(parents=True, exist_ok=True)
        target = root / time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copytree(
                dest, target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "node_modules"),
                symlinks=True,
            )
        except Exception as exc:
            logger.warning("[550C] 备份 %s 失败：%s", pid, exc)
            return None

        # 只留最近几份，别把磁盘喂满
        try:
            olds = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
            for old in olds[KEEP_BACKUPS:]:
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            pass
        return target

    def _restore(self, pid: str, dest: Path, backup) -> bool:
        if not backup or not Path(backup).is_dir():
            return False
        try:
            if Path(dest).exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(backup, dest, symlinks=True)
            logger.warning("[550C] 已把 %s 回滚到更新前那份", pid)
            return True
        except Exception as exc:
            logger.error("[550C] 回滚 %s 失败：%s", pid, exc)
            return False

    # ---------------- 真正干活 ----------------
    async def _apply(self, plan: dict) -> dict:
        """换掉一个插件。成功返回 ok=True，失败会尽量把旧的那份放回去。"""
        from core.plugin.plugin_installer import install_from_github, install_requirements

        mgr = getattr(self.host.ctx, "plugin_mgr", None)
        if mgr is None:
            return {"ok": False, "error": "插件管理器不见了"}
        pid = plan["id"]
        plugins_dir = Path(mgr.plugin_dir)
        try:
            found = mgr.get_plugin_module_path(pid)
        except Exception:
            found = None
        dest = Path(found) if found else plugins_dir / pid
        if not dest.is_dir():
            return {"ok": False, "error": "本地找不到插件目录，跳过"}

        backup = await asyncio.to_thread(self._backup, pid, dest)

        try:
            installed = await install_from_github(
                plan["repo"],
                plugins_dir,
                gh_proxy="auto",
                target_dir=dest,
                expected_plugin_id=pid,
            )
            try:
                warnings = await install_requirements(Path(installed))
            except Exception as exc:  # 依赖装不上不该直接判死刑
                warnings = ["依赖安装异常：%s" % exc]
            for warn in warnings or []:
                logger.warning("[550C] %s 依赖提醒：%s", pid, warn)

            try:
                await mgr.prepare_plugin_reload(pid)
            except Exception:
                pass
            loaded = await mgr.load_plugin_from_dir(Path(installed))
            if str(loaded or "") != pid:
                raise RuntimeError("更新后加载出来的插件身份不对：%s" % loaded)

            info = mgr.get_plugin_info(pid)
            return {
                "ok": True,
                "version": str(getattr(info, "version", "") or plan["to"]),
                "warnings": list(warnings or []),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = "%s: %s" % (type(exc).__name__, exc)
            rolled = await asyncio.to_thread(self._restore, pid, dest, backup)
            if rolled:
                try:
                    await mgr.prepare_plugin_reload(pid)
                    await mgr.load_plugin_from_dir(dest)
                except Exception as rel_exc:
                    message += "；回滚后重载也失败：%s" % rel_exc
                else:
                    message += "；已回滚到旧版本"
            elif backup:
                message += "；回滚失败，请手工看一眼"
            return {"ok": False, "error": message}

    async def run_once(self, manual: bool = False) -> dict:
        """检查一次，并把该更新的都更新掉。"""
        if self._busy:
            return {"ok": False, "error": "上一轮还没跑完"}
        self._busy = True
        cfg = self._cfg()
        started = int(time.time())
        done, failed = [], []
        try:
            market = await self.market(force=manual)
            all_plans = self.plans(market)
            plans = all_plans[: cfg["max_per_run"]]
            logger.info("[550C] 自动更新检查：市场 %d 条，本次处理 %d/%d",
                        len(market), len(plans), len(all_plans))

            for idx, plan in enumerate(plans):
                if idx:
                    await asyncio.sleep(GAP_SECONDS)
                logger.info("[550C] 更新 %s %s → %s", plan["id"], plan["from"], plan["to"])
                result = await self._apply(plan)
                self._state["history"].insert(0, {
                    "ts": int(time.time()),
                    "id": plan["id"],
                    "name": plan["name"],
                    "from": plan["from"],
                    "to": plan["to"],
                    "ok": bool(result.get("ok")),
                    "error": str(result.get("error") or ""),
                    "manual": bool(manual),
                })
                del self._state["history"][MAX_HISTORY:]

                blocked = self._state.setdefault("blocked", {})
                if result.get("ok"):
                    blocked.pop(plan["id"], None)
                    done.append({"id": plan["id"], "to": plan["to"],
                                 "warnings": result.get("warnings") or []})
                else:
                    fails = int(blocked.get(plan["id"]) or 0) + 1
                    blocked[plan["id"]] = fails
                    failed.append({"id": plan["id"], "error": result.get("error") or "未知错误",
                                   "fails": fails})

            cfg2 = self._cfg()
            now = int(time.time())
            self._state["last_check"] = now
            self._state["next_at"] = now + cfg2["interval_h"] * 3600
            if done or failed:
                bits = []
                if done:
                    bits.append("更新 %d 个" % len(done))
                if failed:
                    bits.append("失败 %d 个" % len(failed))
                self._state["last_result"] = "，".join(bits) + "（%s）" % _fmt_ts(now)
            else:
                self._state["last_result"] = "已是最新（%s）" % _fmt_ts(now)
            self._state["last_error"] = failed[0]["error"] if failed else ""
            if done:
                logger.info("[550C] 自动更新完成：%s", ", ".join(d["id"] for d in done))
            if failed:
                logger.warning("[550C] 自动更新失败：%s", failed)
            return {"ok": True, "done": done, "failed": failed,
                    "checked": len(plans), "pending": len(all_plans),
                    "market": self._market_note,
                    "elapsed": int(time.time()) - started}
        except Exception as exc:
            logger.error("[550C] 自动更新本轮出错：%s", exc)
            self._state["last_error"] = "%s: %s" % (type(exc).__name__, exc)
            self._state["last_result"] = "本轮出错（%s）" % _fmt_ts(time.time())
            return {"ok": False, "error": str(exc)}
        finally:
            self._busy = False
            self._save_state()

    async def check_now(self) -> dict:
        return await self.run_once(manual=True)

    def unblock(self, pid: str) -> None:
        (self._state.get("blocked") or {}).pop(str(pid), None)
        self._save_state()

    def reset_schedule(self) -> None:
        cfg = self._cfg()
        self._state["next_at"] = int(time.time()) + cfg["interval_h"] * 3600
        self._save_state()

    # ---------------- 给面板看 ----------------
    def status(self) -> dict:
        cfg = self._cfg()
        market = self._market
        try:
            plans = self.plans(market)
        except Exception:
            plans = []
        blocked = self._state.get("blocked") or {}
        return {
            "enabled": cfg["enabled"],
            "interval_h": cfg["interval_h"],
            "max_per_run": cfg["max_per_run"],
            "exclude": self._protected(),
            "busy": self._busy,
            "last_check": self._state.get("last_check") or 0,
            "last_check_at": _fmt_ts(self._state.get("last_check")),
            "next_at": self._state.get("next_at") or 0,
            "next_check_at": _fmt_ts(self._state.get("next_at")),
            "last_result": self._state.get("last_result") or "-",
            "last_error": self._state.get("last_error") or "",
            "market_note": self._market_note or "尚未拉到市场清单",
            "market_size": len(market),
            "pending": plans,
            "blocked": [{"id": k, "fails": int(v or 0)} for k, v in blocked.items()],
            "history": (self._state.get("history") or [])[:8],
        }
