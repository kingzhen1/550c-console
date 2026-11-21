"""550C 框架自动更新器。

和 updater.py（只管插件）分开写，互不打扰：
* 去看 KiraAI-Dev/KiraAI 的 release，只认比当前 VERSION 新的
* 源码包 + WebUI dist 包一起换，换之前留整份备份
* 装依赖、编译自检、导入冒烟，全过才提交
* 任何一步不对就原地回滚，绝不带着半成品重启
* 全都过了才 os._exit(42)，supervisor 会把新进程拉起来

默认是关的。换框架要重启整个天网，得自己点开才动。
"""

from __future__ import annotations

import asyncio
import compileall
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from core.plugin import logger

OWNER = "xxynet"
REPO = "KiraAI"

# supervisor 认得这个退出码：42 = 自己要求重启
RESTART_EXIT_CODE = 42
RESTART_DELAY_SECONDS = 1.5
# 主循环心跳，真正什么时候动看 next_at
LOOP_TICK = 60
# release 列表缓存半小时
RELEASES_TTL = 1800
# 留几份备份
KEEP_BACKUPS = 2

DEFAULTS = {
    "fw_auto_update": False,
    "fw_update_interval_hours": 24,
    "fw_allow_prerelease": True,
    "fw_github_token": "",
}


def _data_root() -> Path:
    try:
        from core.utils.path_utils import get_data_path

        return Path(get_data_path())
    except Exception:
        return Path(__file__).resolve().parents[2]


def _short_err(exc: BaseException) -> str:
    """错误名说得清的就用错误名，说不太清的带上 HTTP 状态码，别再一串裸异常名。"""
    name = type(exc).__name__
    if name == "HTTPStatusError":
        try:
            return "HTTP %d" % int(exc.response.status_code)  # type: ignore[attr-defined]
        except Exception:
            return "HTTP 状态异常"
    if name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException"):
        return "连不上 GitHub"
    return name


def _shared_github_token() -> str:
    """框架更新自己没配 token 时，借 github-tool 那份。

    核心那套 github_api 是匿名请求，一小时只有 60 次，检查几轮就被 403，
    面板上就变成“拉 release 失败”、最新版本显示成 -，白瞎。
    """
    try:
        p = _data_root() / "config" / "plugins" / "github-tool.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        return str((data or {}).get("github_token") or "").strip()
    except Exception:
        return ""


def _work_dir() -> Path:
    d = _data_root() / "plugin_autoupdate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def current_version() -> str:
    try:
        from core.config.default import VERSION

        return str(VERSION or "-")
    except Exception:
        return "-"


def _norm(tag: str) -> str:
    return str(tag or "").strip().lstrip("vV")


def _newer(latest: str, local: str) -> bool:
    """latest 是不是真的比 local 新。解析不了就退回字符串比较。"""
    a, b = _norm(latest), _norm(local)
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


def _hard_exit() -> None:
    """绕过 atexit / uvicorn 的优雅关闭，直接把进程交给 supervisor。"""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(RESTART_EXIT_CODE)


class FrameworkUpdater:
    """到点看一眼本体有没有新版，有就换掉，然后让 supervisor 重启。"""

    def __init__(self, host):
        self.host = host
        self._task = None
        self._busy = False
        self._releases = []
        self._releases_at = 0.0
        self._note = ""
        # 这一轮到底有没有真拉到：拉取失败时 pick() 只能拿旧缓存瞎猜，
        # 面板上的“已是最新”就成了假消息，得拿这个标记兜住
        self._fetch_ok = True
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

        return {
            "enabled": bool(cfg.get("fw_auto_update", DEFAULTS["fw_auto_update"])),
            "interval_h": _int("fw_update_interval_hours", 1, 168),
            "prerelease": bool(cfg.get("fw_allow_prerelease", DEFAULTS["fw_allow_prerelease"])),
            "token": str(cfg.get("fw_github_token") or "").strip() or _shared_github_token(),
        }

    # ---------------- 状态 ----------------
    def _state_path(self) -> Path:
        return _work_dir() / "framework_state.json"

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        pending = data.get("pending")
        rows = data.get("history")
        return {
            "last_check": int(data.get("last_check") or 0),
            "next_at": int(data.get("next_at") or 0),
            "last_result": str(data.get("last_result") or ""),
            "last_error": str(data.get("last_error") or ""),
            "last_backup": str(data.get("last_backup") or ""),
            "history": rows if isinstance(rows, list) else [],
            "pending": pending if isinstance(pending, dict) else None,
        }

    def _save_state(self) -> None:
        try:
            self._state_path().write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("[550C] 框架更新状态写不下去：%s", exc)

    def _settle_pending(self) -> None:
        """上次如果死在换文件那一步，重启后在这里对账。"""
        pending = self._state.get("pending")
        if not pending:
            return
        tag = str(pending.get("tag") or "")
        now_ver = current_version()
        if _norm(now_ver) == _norm(tag):
            self._state["last_result"] = "已更新到 %s（%s）" % (tag, _fmt_ts(time.time()))
            self._state["last_error"] = ""
            self._state["history"].insert(0, {"ts": int(time.time()), "tag": tag, "ok": True})
            del self._state["history"][20:]
            logger.info("[550C] 框架更新对账：已经是 %s，算成功", tag)
        else:
            self._state["last_result"] = "更新到 %s 没生效，当前仍是 %s" % (tag, now_ver)
            self._state["last_error"] = str(pending.get("error") or "重启后版本没变")
            self._state["history"].insert(
                0, {"ts": int(time.time()), "tag": tag, "ok": False,
                    "error": self._state["last_error"]}
            )
            del self._state["history"][20:]
            logger.warning("[550C] 框架更新对账：目标 %s，实际 %s", tag, now_ver)
        self._state["pending"] = None
        self._save_state()

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        self._settle_pending()
        cfg = self._cfg()
        now = int(time.time())
        span = cfg["interval_h"] * 3600
        if not self._state.get("next_at") or self._state["next_at"] < now - span:
            self._state["next_at"] = now + span
            self._save_state()
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="550c-fwupdate")
        logger.info(
            "[550C] 框架更新已挂上：%s，每 %d 小时一次，当前 %s，下次 %s",
            "开启" if cfg["enabled"] else "关闭",
            cfg["interval_h"],
            current_version(),
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
                if not cfg["enabled"]:
                    await asyncio.sleep(LOOP_TICK)
                    continue
                now = time.time()
                next_at = float(self._state.get("next_at") or 0)
                if now >= next_at:
                    await self.run_once(manual=False)
                    continue
                await asyncio.sleep(min(LOOP_TICK, max(5.0, next_at - now)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 循环本身不能死
                logger.error("[550C] 框架更新循环抛错：%s", exc)
                await asyncio.sleep(LOOP_TICK)

    # ---------------- releases ----------------
    async def _fetch_releases(self) -> list:
        """拉 release 列表。

        带 token 就自己发请求——核心那套 github_api 不带认证，
        匿名一小时只有 60 次，检查几轮就被 403 限流，框架更新等于形同虚设。
        没填 token 就退回核心那套，至少能通。
        """
        token = self._cfg()["token"]
        if not token:
            from core.utils.github_api import get_all_releases

            return await get_all_releases(OWNER, REPO)

        import httpx

        headers = {
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "550C-fwupdater",
        }
        out: list = []
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
            for page in range(1, 4):
                url = ("https://api.github.com/repos/%s/%s/releases?per_page=30&page=%d"
                       % (OWNER, REPO, page))
                resp = await client.get(url)
                if 300 <= resp.status_code < 400:
                    # 仓库改名/迁移时 api 会回 301，跟随失败就会拿到一个空壳，
                    # 上层把空列表当成“没有新版”，面板还美滋滋显示已是最新
                    raise RuntimeError("仓库地址被重定向（HTTP %d），OWNER/REPO 需要更新"
                                       % resp.status_code)
                if resp.status_code in (401, 403):
                    raise RuntimeError("GitHub 拒绝请求（token 无效或已限流，HTTP %d）"
                                       % resp.status_code)
                resp.raise_for_status()
                batch = resp.json()
                if isinstance(batch, dict):
                    raise RuntimeError("release 接口返回了错误对象：%s"
                                       % str(batch.get("message") or "未知")[:120])
                if not isinstance(batch, list) or not batch:
                    break
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    out.append({
                        "tag_name": item.get("tag_name"),
                        "name": item.get("name"),
                        "body": item.get("body"),
                        "html_url": item.get("html_url"),
                        "published_at": item.get("published_at"),
                        "prerelease": item.get("prerelease"),
                        "draft": item.get("draft"),
                    })
                if len(batch) < 30:
                    break
        return out

    async def releases(self, force: bool = False) -> list:
        now = time.time()
        if not force and self._releases and now - self._releases_at < RELEASES_TTL:
            return self._releases
        try:
            data = await self._fetch_releases()
        except Exception as exc:
            self._fetch_ok = False
            if self._releases:
                self._note = "这轮没拉到 release（%s），沿用 %s 的缓存" % (
                    _short_err(exc), _fmt_ts(self._releases_at))
                logger.warning("[550C] 拉框架 release 失败：%s", exc)
                return self._releases
            self._note = "这轮没拉到 release（%s），稍后自动重试" % _short_err(exc)
            logger.warning("[550C] 拉框架 release 失败：%s", exc)
            return []
        self._fetch_ok = True
        items = [r for r in (data or []) if isinstance(r, dict) and not r.get("draft")]
        self._releases, self._releases_at = items, now
        self._note = "在线拉取 %s（%d 条）" % (_fmt_ts(now), len(items))
        return items

    def pick(self, releases: list, allow_prerelease: bool) -> dict | None:
        """挑出该上的那一个，只认比当前 VERSION 新的。"""
        local = current_version()
        best = None
        for item in releases:
            tag = str(item.get("tag_name") or "").strip()
            if not tag:
                continue
            if item.get("prerelease") and not allow_prerelease:
                continue
            if not _newer(tag, local):
                continue
            if best is None or _newer(tag, str(best.get("tag_name") or "")):
                best = item
        if best is None:
            return None
        return {
            "tag": str(best.get("tag_name") or ""),
            "name": str(best.get("name") or best.get("tag_name") or ""),
            "published_at": str(best.get("published_at") or ""),
            "prerelease": bool(best.get("prerelease")),
            "from": local,
        }

    # ---------------- 自检 / 备份 ----------------
    async def _selftest(self) -> None:
        """换完文件、重启之前，先自己验一遍新代码能不能跑起来。"""
        from core.utils.path_utils import get_root_path

        root = get_root_path()
        for sub in ("core", "webui"):
            target = Path(root) / sub
            if not target.is_dir():
                continue
            ok = await asyncio.to_thread(compileall.compile_dir, str(target), quiet=1)
            if not ok:
                raise RuntimeError("新代码编译不过：%s" % sub)

        probe = (
            "import core.config.default as c, core.lifecycle, core.plugin, webui.app;"
            "print(c.VERSION)"
        )
        proc = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", probe],
            capture_output=True,
            timeout=180,
            cwd=str(root),
        )
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            raise RuntimeError("新代码导入自检失败：%s" % (tail or "无输出"))
        logger.info("[550C] 框架自检通过：%s", (proc.stdout or b"").decode().strip())

    def _stash_backup(self, tx) -> str:
        """把这次更新留的备份另存一份，万一新版本起不来还能手工找回旧文件。"""
        src = getattr(tx, "backup_dir", None)
        if not src or not Path(src).is_dir():
            return ""
        dest = _work_dir() / "fw_last_backup"
        try:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest, symlinks=True)
            return str(dest)
        except Exception as exc:
            logger.warning("[550C] 另存框架备份失败：%s", exc)
            return ""

    def _prune_old_backups(self) -> None:
        try:
            root = _work_dir() / "fw_backups"
            if not root.is_dir():
                return
            olds = sorted([p for p in root.iterdir() if p.is_dir()],
                          key=lambda p: p.name, reverse=True)
            for old in olds[KEEP_BACKUPS:]:
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            pass

    async def _prepare_zip(self, tag: str) -> Path:
        """拿源码包。有缓存就直接用，没有就挑最快的源下载。"""
        from core.utils.github_api import download_asset, pick_fastest_source

        updates_dir = _data_root() / "updates"
        updates_dir.mkdir(parents=True, exist_ok=True)
        safe_tag = "".join(ch if ch.isalnum() or ch in "-._" else "-" for ch in tag)[:128] or "unknown"
        zip_path = updates_dir / ("%s.zip" % safe_tag)
        if zip_path.exists() and zip_path.stat().st_size > 0:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    if zf.testzip() is None:
                        logger.info("[550C] 复用已下载的 %s", zip_path.name)
                        return zip_path
            except Exception:
                pass
            zip_path.unlink(missing_ok=True)

        direct = "https://github.com/%s/%s/archive/refs/tags/%s.zip" % (OWNER, REPO, tag)
        urls = await pick_fastest_source(direct)
        if not urls:
            raise RuntimeError("所有下载源都试过了，没有一个通")
        data = None
        for idx, url in enumerate(urls):
            try:
                data = await download_asset(url)
                logger.info("[550C] 框架源码包已下载（源 %d/%d，%.1f MB）",
                            idx + 1, len(urls), len(data) / 1024 / 1024)
                break
            except Exception as exc:
                logger.warning("[550C] 源码源 %d 失败：%s", idx + 1, exc)
        if data is None:
            raise RuntimeError("源码包下载失败")
        await asyncio.get_running_loop().run_in_executor(None, zip_path.write_bytes, data)
        return zip_path

    # ---------------- 干活 ----------------
    async def _apply(self, tag: str) -> dict:
        """把本体换到 tag。成功会安排重启。"""
        from core.plugin.plugin_installer import install_requirements
        from core.utils.dist_checker import apply_dist_archive, download_dist_archive
        from core.utils.path_utils import get_root_path
        from webui.routes.releases import ReleasesRoutes

        zip_path = await self._prepare_zip(tag)
        dist_bytes = await download_dist_archive(tag)
        if not dist_bytes:
            raise RuntimeError("WebUI 包是空的")

        self._state["pending"] = {"tag": tag, "ts": int(time.time()), "error": ""}
        self._save_state()

        source_tx = None
        dist_tx = None
        try:
            source_tx = await asyncio.to_thread(ReleasesRoutes._apply_update, zip_path, True)
            dist_tx = await asyncio.to_thread(apply_dist_archive, dist_bytes, tag, True)

            config = getattr(getattr(self.host, "ctx", None), "config", None)
            mirror = ((config.get("network") or {}).get("pypi_mirror") if config else None)
            warnings = await install_requirements(get_root_path(), pypi_mirror=mirror)
            if warnings:
                raise RuntimeError("依赖安装失败：" + "; ".join(warnings))

            await self._selftest()

            backup_copy = await asyncio.to_thread(self._stash_backup, source_tx)
            if backup_copy:
                self._state["last_backup"] = backup_copy
                self._save_state()

            await asyncio.to_thread(source_tx.commit)
            source_tx = None
            await asyncio.to_thread(dist_tx.commit)
            dist_tx = None
        except Exception:
            for tx in (dist_tx, source_tx):
                if tx is None:
                    continue
                try:
                    errs = await asyncio.to_thread(tx.rollback)
                    for msg in errs:
                        logger.error("[550C] 框架回滚：%s", msg)
                except Exception as exc:
                    logger.exception("[550C] 框架回滚失败：%s", exc)
            raise

        self._state["last_result"] = "已换到 %s，等重启" % tag
        self._save_state()
        logger.info("[550C] 框架文件已就位（%s），%s 秒后重启", tag, RESTART_DELAY_SECONDS)
        asyncio.get_running_loop().call_later(RESTART_DELAY_SECONDS, _hard_exit)
        return {"ok": True, "tag": tag, "restarting": True}

    async def run_once(self, manual: bool = False) -> dict:
        """检查一次，有新版就换。"""
        if self._busy:
            return {"ok": False, "error": "上一轮还没跑完"}
        self._busy = True
        cfg = self._cfg()
        started = int(time.time())
        try:
            releases = await self.releases(force=manual)
            target = self.pick(releases, cfg["prerelease"])
            now = int(time.time())
            if target is None:
                self._state["last_check"] = now
                self._state["next_at"] = now + cfg["interval_h"] * 3600
                if self._fetch_ok:
                    self._state["last_result"] = "已是最新（%s，当前 %s）" % (
                        _fmt_ts(now), current_version())
                    self._state["last_error"] = ""
                    note = "已是最新"
                else:
                    # 连 release 都没拉到，“已是最新”就是自己骗自己
                    self._state["last_result"] = "这轮没拉到 release，判断不了（%s）" % _fmt_ts(now)
                    self._state["last_error"] = ""
                    note = "没拉到 release"
                self._save_state()
                return {"ok": True, "done": False, "note": note,
                        "current": current_version(), "market": self._note}

            logger.info("[550C] 框架要更新：%s → %s", target["from"], target["tag"])
            try:
                result = await self._apply(target["tag"])
            except Exception as exc:
                message = "%s: %s" % (type(exc).__name__, exc)
                self._state["pending"] = None
                self._state["last_check"] = int(time.time())
                self._state["next_at"] = int(time.time()) + cfg["interval_h"] * 3600
                self._state["last_result"] = "更新 %s 失败，已回滚" % target["tag"]
                self._state["last_error"] = message
                self._state["history"].insert(0, {
                    "ts": int(time.time()), "tag": target["tag"], "ok": False, "error": message,
                })
                del self._state["history"][20:]
                logger.error("[550C] 框架更新失败：%s", message)
                return {"ok": False, "error": message, "tag": target["tag"]}

            self._state["last_check"] = int(time.time())
            self._state["history"].insert(0, {
                "ts": int(time.time()), "tag": target["tag"], "ok": True,
                "from": target["from"], "manual": bool(manual),
            })
            del self._state["history"][20:]
            self._prune_old_backups()
            return {"ok": True, "done": True, "tag": target["tag"],
                    "restarting": True, "elapsed": int(time.time()) - started, **result}
        except Exception as exc:
            message = "%s: %s" % (type(exc).__name__, exc)
            self._state["last_error"] = message
            self._state["last_result"] = "本轮出错（%s）" % _fmt_ts(time.time())
            logger.error("[550C] 框架更新本轮出错：%s", message)
            return {"ok": False, "error": message}
        finally:
            self._busy = False
            self._save_state()

    async def check_now(self) -> dict:
        return await self.run_once(manual=True)

    def reset_schedule(self) -> None:
        cfg = self._cfg()
        self._state["next_at"] = int(time.time()) + cfg["interval_h"] * 3600
        self._save_state()

    # ---------------- 给面板看 ----------------
    def status(self) -> dict:
        cfg = self._cfg()
        local = current_version()
        try:
            target = self.pick(self._releases, cfg["prerelease"]) if self._releases else None
        except Exception:
            target = None
        # 没有新版时“最新 release”也别空着，给个实际拉到的最新 tag
        newest = "-"
        for r in self._releases or []:
            tag = str(r.get("tag_name") or "").strip()
            if not tag or (r.get("prerelease") and not cfg["prerelease"]):
                continue
            if newest == "-" or _newer(tag, newest):
                newest = tag
        return {
            "enabled": cfg["enabled"],
            "interval_h": cfg["interval_h"],
            "prerelease": cfg["prerelease"],
            "busy": self._busy,
            "current": _norm(local) or local,   # 面板自己会补一个 v，别变成 vv3.1.2
            "latest": (target or {}).get("tag") or newest,
            "has_new": bool(target),
            "next_check_at": _fmt_ts(self._state.get("next_at")),
            "last_check_at": _fmt_ts(self._state.get("last_check")),
            "last_result": self._state.get("last_result") or "-",
            "last_error": self._state.get("last_error") or "",
            "last_backup": self._state.get("last_backup") or "",
            "market_note": self._note or "还没拉过 release",
            "releases": [
                {
                    "tag": str(r.get("tag_name") or ""),
                    "name": str(r.get("name") or ""),
                    "published_at": str(r.get("published_at") or ""),
                    "prerelease": bool(r.get("prerelease")),
                }
                for r in (self._releases or [])[:10]
            ],
            "history": (self._state.get("history") or [])[:6],
        }
