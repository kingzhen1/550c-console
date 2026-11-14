"""550C 开机动画 — KiraAI 面板插件。

播放页 web/show.html 由 scripts/gen.py 从原稿分析生成，
本文件只做三件事：把 /console 挂进侧边栏、把 web 目录下发、
以及读写面板上的几项首选项。
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

from core.plugin import BasePlugin, PluginContext, register, PageMenu, PluginPage, logger

try:
    from .updater import AutoUpdater
except ImportError:  # 离线自测时本文件不是包，退回平级导入
    from updater import AutoUpdater

PLUGIN_ID = "kira-ai-plugin-550c-boot"

MODE_OPTIONS = ("simple", "full", "off")
SCHEME_OPTIONS = ("amber", "green", "cyan", "white")
DEFAULTS = {
    "mode": "full",
    "scheme": "amber",
    "auto_play": True,
    "loop": False,
    "duration_hint": 24,
    # 告警默认值：阈值按“先能看见”给，不想被打扰可以直接关掉总开关
    "alert_enabled": True,
    "alert_disk": 90,
    "alert_mem": 90,
    "alert_cpu": 95,
    "alert_webhook": "",
    # 趋势图保留小时数
    "trend_hours": 24,
}

# 报错归并时抹掉的噪声：时间戳、端口、计数、十六进制地址这类每次都变的部分。
# 不抹掉的话“重连失败 59 次”和“重连失败 42 次”会被当成两类报错。
_ERR_NOISE = (
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b\d+\.\d+\.\d+\.\d+(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:秒|分钟|小时|s|ms|次|个|条|字节|KB|MB|GB|B)\b"), "<n>"),
    (re.compile(r"\b\d+\b"), "<n>"),
)


def _err_key(src: str, msg: str) -> str:
    """把一条报错压成稳定的归并键：同一类反复刷屏应该合成一行。"""
    text = f"{src or ''}|{msg or ''}"
    for pattern, repl in _ERR_NOISE:
        text = pattern.sub(repl, text)
    return text[:160]


def _err_head(msg: str, width: int = 72) -> str:
    """归并行的显示标题，取归一化后最有辨识度的开头。"""
    clean = msg or ""
    for pattern, repl in _ERR_NOISE:
        clean = pattern.sub(repl, clean)
    clean = " ".join(clean.split())
    return clean[:width]


# ---------------------------------------------------------------------------
# 内置价目表：人民币，元 / 每百万 token
#   in     输入（未命中缓存）   cached  输入命中缓存   out  输出
# 匹配取「最长命中」，所以 gpt-4o-mini 不会被 gpt-4o 抢走。
# 想覆盖或补价：写 data/plugin_data/<插件ID>/prices.json，形如
#   {"我的模型名": {"in": 1, "out": 2, "cached": 0.1}, "_default": {...}}
# 加了 _default 就当兜底价，认不出来的模型也照它算。
# ---------------------------------------------------------------------------
DEFAULT_PRICES = {
    # DeepSeek：官方人民币定价，缓存命中价单独一列
    "deepseek-chat": {"in": 2.0, "out": 8.0, "cached": 0.5},
    "deepseek-v3": {"in": 2.0, "out": 8.0, "cached": 0.5},
    "deepseek-reasoner": {"in": 4.0, "out": 16.0, "cached": 1.0},
    "deepseek-r1": {"in": 4.0, "out": 16.0, "cached": 1.0},
    # OpenAI：美元价按 7.2 折人民币
    "gpt-5-mini": {"in": 1.8, "out": 14.4, "cached": 0.18},
    "gpt-5": {"in": 9.0, "out": 72.0, "cached": 0.9},
    "gpt-4.1-nano": {"in": 0.72, "out": 2.9, "cached": 0.18},
    "gpt-4.1-mini": {"in": 2.9, "out": 11.6, "cached": 0.72},
    "gpt-4.1": {"in": 14.4, "out": 57.6, "cached": 3.6},
    "gpt-4o-mini": {"in": 1.1, "out": 4.32, "cached": 0.55},
    "gpt-4o": {"in": 18.0, "out": 72.0, "cached": 9.0},
    "o4-mini": {"in": 7.9, "out": 31.7, "cached": 2.0},
    "o3": {"in": 14.4, "out": 57.6, "cached": 3.6},
    # Anthropic
    "claude-haiku": {"in": 5.8, "out": 28.8, "cached": 0.58},
    "claude-3-5-sonnet": {"in": 21.6, "out": 108.0, "cached": 2.16},
    "claude-3-7-sonnet": {"in": 21.6, "out": 108.0, "cached": 2.16},
    "claude-sonnet-4": {"in": 21.6, "out": 108.0, "cached": 2.16},
    "claude-opus-4": {"in": 108.0, "out": 540.0, "cached": 10.8},
    # Google
    "gemini-2.5-pro": {"in": 9.0, "out": 72.0, "cached": 2.25},
    "gemini-2.5-flash": {"in": 2.16, "out": 18.0, "cached": 0.54},
    "gemini-2.0-flash": {"in": 0.72, "out": 2.9, "cached": 0.18},
    # 国内厂商
    "qwen-max": {"in": 2.4, "out": 9.6, "cached": 0.48},
    "qwen-plus": {"in": 0.8, "out": 2.0, "cached": 0.16},
    "qwen-turbo": {"in": 0.3, "out": 0.6, "cached": 0.06},
    "glm-4-plus": {"in": 5.0, "out": 5.0, "cached": 1.0},
    "glm-4-flash": {"in": 0.0, "out": 0.0, "cached": 0.0},
    "glm-4": {"in": 1.0, "out": 1.0, "cached": 0.2},
    "moonshot-v1-8k": {"in": 12.0, "out": 12.0, "cached": 2.4},
    "kimi-k2": {"in": 4.0, "out": 16.0, "cached": 0.8},
    "doubao": {"in": 0.8, "out": 2.0, "cached": 0.16},
    "hunyuan": {"in": 1.0, "out": 2.0, "cached": 0.2},
    "spark": {"in": 0.0, "out": 0.0, "cached": 0.0},
}

_PRICE_FILE = "prices.json"


def _price_file() -> Path:
    """用户价目表：data/plugin_data/<插件ID>/prices.json。"""
    try:
        from core.utils.path_utils import get_data_path

        return Path(get_data_path()) / "plugin_data" / PLUGIN_ID / _PRICE_FILE
    except Exception:
        return Path(__file__).parent / _PRICE_FILE


def _norm_price(raw: dict) -> dict:
    """统一成 {in, out, cached}，缺 cached 就按输入价（保守，不虚报）。"""
    def num(key, dflt=0.0):
        try:
            return float(raw.get(key, dflt) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    in_p = num("in", num("input", 0.0))
    return {
        "in": in_p,
        "out": num("out", num("output", 0.0)),
        "cached": num("cached", num("cache", in_p)),
    }


def _load_prices() -> dict:
    """内置表打底，用户文件逐条覆盖；读坏了也只是少几条价，不影响面板。"""
    table = {k.lower(): dict(v) for k, v in DEFAULT_PRICES.items()}
    path = _price_file()
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, val in raw.items():
                    if isinstance(val, dict):
                        table[str(key).lower()] = _norm_price(val)
    except Exception as exc:
        logger.error("[550C] 读价目表失败：%s", exc)
    return table


def _price_of(model: str, table: dict) -> dict | None:
    """先精确、再最长片段命中；都没有就看有没有 _default 兜底。"""
    name = str(model or "").strip().lower()
    if not name:
        return table.get("_default")
    if name in table:
        return table[name]
    tail = name.split(":")[-1]
    if tail in table:
        return table[tail]
    hit = ""
    for key in table:
        if key == "_default" or not key:
            continue
        if key in name and len(key) > len(hit):
            hit = key
    if hit:
        return table[hit]
    return table.get("_default")


def _cost_of(row: dict, price: dict | None) -> float | None:
    """一次调用的钱：缓存命中的输入单独计价，其余按新输入价。"""
    if not price:
        return None
    try:
        inp = max(0, int(row.get("input_tokens", 0) or 0))
        out = max(0, int(row.get("output_tokens", 0) or 0))
        cached = max(0, int(row.get("cached_tokens", 0) or 0))
    except (TypeError, ValueError):
        return None
    if cached > inp:
        cached = inp
    fresh = inp - cached
    return (
        fresh * float(price.get("in", 0.0))
        + cached * float(price.get("cached", 0.0))
        + out * float(price.get("out", 0.0))
    ) / 1_000_000.0



# 日志格式见 core/logging_manager.py：
#   %(asctime)s %(levelname)-8s [%(name)s] %(message)s
_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(ERROR|CRITICAL|WARNING)\s+\[([^\]]*)\]\s?(.*)$"
)
_ERROR_LEVELS = ("ERROR", "CRITICAL", "WARNING")


class Boot550CPlugin(BasePlugin):

    def __init__(self, ctx: PluginContext, cfg: dict):
        super().__init__(ctx, cfg)
        self.plugin_cfg = cfg or {}
        self.web_dir = Path(__file__).parent / "web"
        # 每个适配器的首次在线时刻，用来在状态面板上算“已在线多久”
        self._online_since: dict = {}
        # 采样节流：面板开多个标签页时不重复记点
        self._last_sample_at = 0.0
        # 告警冷却：同一条告警在窗口内只推一次，避免刷屏
        self._alert_sent: dict = {}
        # 自动更新器：initialize 里挂上，terminate 里摘掉
        self.updater = None
        self._warm_cpu()

    @staticmethod
    def _warm_cpu():
        """psutil 的 cpu_percent 第一次调用必然是 0，先热一下，面板首帧才不骗人。"""
        try:
            import psutil

            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    def _adapter_rows(self) -> list:
        """适配器运行时快照。取的是真实例，不是配置里的 enabled 字样。"""
        mgr = getattr(self.ctx, "adapter_mgr", None)
        if mgr is None:
            return []
        try:
            infos = mgr.get_adapters_info()
        except Exception as exc:
            logger.error("[550C] 读取适配器列表失败：%s", exc)
            return []

        runtime = getattr(mgr, "_adapters", None) or {}
        tasks = getattr(mgr, "_adapter_tasks", None) or {}
        now = time.time()
        rows = []

        for info in infos:
            inst = runtime.get(info.name) or runtime.get(info.adapter_id)
            row = {
                "id": info.adapter_id,
                "name": info.name or info.adapter_id,
                "platform": info.platform or "-",
                "enabled": bool(info.enabled),
                "live": inst is not None,
                "starting": bool(tasks.get(info.name or info.adapter_id)),
            }
            if inst is None:
                self._online_since.pop(info.adapter_id, None)
                rows.append(row)
                continue

            cfg = getattr(inst, "config", None) or {}
            bot = getattr(inst, "bot", None)
            row["dead"] = bool(getattr(inst, "permanently_disconnected", False))
            row["link"] = getattr(bot, "ws_url", None) or cfg.get("ws_uri") or "-"
            if bot is not None:
                row["socket"] = getattr(bot, "websocket", None) is not None
                event = getattr(bot, "login_success_event", None)
                try:
                    row["logged_in"] = bool(event.is_set()) if event is not None else False
                except Exception:
                    row["logged_in"] = False
                row["uin"] = str(getattr(bot, "self_id", None) or cfg.get("bot_pid") or "-")
            else:
                row["socket"] = row["live"]
                row["logged_in"] = None
                row["uin"] = str(cfg.get("bot_pid") or "-")

            since = self._online_since.setdefault(info.adapter_id, now)
            row["online_s"] = int(now - since)
            rows.append(row)

        return rows

    # ---- 模型 / 用量：给面板的第二个状态卡 ----------------
    # 默认位对应的模型键（顺序即展示顺序），拿不到就当没有，不影响其它字段
    _DEFAULT_KEYS = (
        "default_llm",
        "default_fast_llm",
        "default_vlm",
        "default_tts",
        "default_stt",
        "default_image",
        "default_video",
        "default_embedding",
        "default_rerank",
    )

    def _providers_cfg(self) -> dict:
        """provider 配置树。换过配置宿主的版本也能落到，都不行就返回空。"""
        cands = []
        cfg = getattr(self.ctx, "config", None)
        if isinstance(cfg, dict):
            cands.append(cfg)
        cfg = getattr(self.ctx, "kira_config", None)
        if isinstance(cfg, dict):
            cands.append(cfg)
        lc = getattr(self.ctx, "lifecycle", None)
        cfg = getattr(lc, "kira_config", None) if lc is not None else None
        if isinstance(cfg, dict):
            cands.append(cfg)
        for c in cands:
            providers = c.get("providers")
            if isinstance(providers, dict):
                return providers
        return {}

    def _defaults_map(self) -> dict:
        """{'<类型>:<模型名>': ['default_llm', ...]}，用来在清单上标出默认位。"""
        mgr = getattr(self.ctx, "provider_mgr", None)
        if mgr is None or not hasattr(mgr, "get_default_model_info"):
            return {}
        out = {}
        for key in self._DEFAULT_KEYS:
            try:
                info = mgr.get_default_model_info(key)
            except Exception:
                continue  # 没配就是没配，不是错
            if info is None:
                continue
            mtype = getattr(info.model_type, "value", info.model_type)
            out.setdefault(f"{mtype}:{info.model_id}", []).append(key)
        return out

    def _model_rows(self) -> dict:
        """按 provider 分组的模型清单。

        优先问运行时管理器（口径跟面板一致），问不到就退回配置树；
        两路都拿不到时返回空清单，面板会自己显示“没读到”，不至于炸。
        """
        providers_cfg = self._providers_cfg()
        mgr = getattr(self.ctx, "provider_mgr", None)
        defaults = self._defaults_map()

        try:
            ids = list(mgr.get_all_providers().keys()) if mgr is not None else []
        except Exception as exc:
            logger.error("[550C] 读取 provider 列表失败：%s", exc)
            ids = []
        for pid in providers_cfg.keys():
            if pid not in ids:
                ids.append(pid)

        runtime = getattr(mgr, "_providers", None) or {}
        rows = []
        total = 0
        for pid in ids:
            meta = providers_cfg.get(pid) or {}
            try:
                groups = mgr.get_models(pid) if mgr is not None else None
            except Exception:
                groups = None
            if not isinstance(groups, dict):
                groups = meta.get("model_config") or {}
            models = []
            for mtype, group in (groups or {}).items():
                if not isinstance(group, dict):
                    continue
                for mid in group.keys():
                    models.append({
                        "type": str(mtype),
                        "id": str(mid),
                        "default": defaults.get(f"{mtype}:{mid}", []),
                    })
            total += len(models)
            rows.append({
                "id": pid,
                "name": meta.get("name") or pid,
                "format": meta.get("format") or "-",
                "live": pid in runtime,
                "count": len(models),
                "models": models,
            })

        rows.sort(key=lambda r: (-r["count"], r["name"]))
        return {"providers": rows, "total": total, "count": len(rows)}

    async def _token_row(self) -> dict:
        """近 24 小时模型用量。宿主有统计就读，没有就坦白说没有。"""
        db = getattr(self.ctx, "db", None)
        fn = getattr(db, "get_llm_summary", None)
        if not callable(fn):
            return {"ok": False, "msg": "当前版本没有用量统计接口"}
        try:
            s = await fn(int(time.time()) - 86400)
        except Exception as exc:
            logger.error("[550C] 采集模型用量失败：%s", exc)
            return {"ok": False, "msg": str(exc)}
        if not isinstance(s, dict):
            return {"ok": False, "msg": "统计接口返回了意外结构"}

        calls = int(s.get("total_calls", 0) or 0)
        total_in = int(s.get("total_input_tokens", 0) or 0)
        total_out = int(s.get("total_output_tokens", 0) or 0)

        table = _load_prices()
        by_model = []
        unpriced = []
        cost_sum = 0.0
        for r in (s.get("by_model") or []):
            if not isinstance(r, dict):
                continue
            model = str(r.get("model") or "-")
            price = _price_of(model, table)
            money = _cost_of(r, price)
            if money is None:
                unpriced.append(model)
            else:
                cost_sum += money
            by_model.append({
                "model": model,
                "calls": int(r.get("calls", 0) or 0),
                "input": int(r.get("input_tokens", 0) or 0),
                "output": int(r.get("output_tokens", 0) or 0),
                "cached": int(r.get("cached_tokens", 0) or 0),
                "avg_ms": r.get("avg_response_ms", 0),
                "price": price,
                "cost": None if money is None else round(money, 6),
            })

        by_model.sort(key=lambda x: -(x["input"] + x["output"]))
        return {
            "ok": True,
            "window": "24h",
            "calls": calls,
            "success": int(s.get("success_count", 0) or 0),
            "input": total_in,
            "output": total_out,
            "cached": int(s.get("total_cached_tokens", 0) or 0),
            "total": total_in + total_out,
            "avg_ms": round(int(s.get("total_response_ms", 0) or 0) / calls, 1) if calls else 0,
            "currency": "CNY",
            "unit": "元/百万 token",
            "cost": round(cost_sum, 6),
            "unknown_price": unpriced,
            "price_file": str(_price_file()),
            "by_model": by_model,
        }

    @staticmethod
    def _log_file() -> Path:
        """拿宿主正在写的那份日志。拿不到就按默认 data/log.log 猜。"""
        try:
            from core import logging_manager as lm

            live = getattr(lm, "_log_file_path", None)
            if live:
                return Path(live)
        except Exception:
            pass
        try:
            from core.utils.path_utils import get_data_path

            return Path(get_data_path()) / "log.log"
        except Exception:
            return Path("data/log.log")

    @staticmethod
    def _tail_lines(path: Path, max_bytes: int = 2 * 1024 * 1024, chunk: int = 128 * 1024) -> list:
        """从文件尾部往回读，只取最后那一天的行，别为了一眼报错把 10MB 日志全吞进来。"""
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                buf = b""
                read = 0
                while pos > 0 and read < max_bytes:
                    step = min(chunk, pos)
                    pos -= step
                    f.seek(pos)
                    buf = f.read(step) + buf
                    read += step
                    head = buf.split(b"\n", 1)[0][:10].decode("utf-8", "ignore")
                    # 读到更早的日期就收手：文件是时间升序的
                    if len(head) == 10 and head[:4].isdigit() and head < time.strftime("%Y-%m-%d"):
                        break
        except Exception as exc:
            logger.error("[550C] 读日志失败：%s", exc)
            return []

        lines = buf.decode("utf-8", "ignore").split("\n")
        dated = [ln for ln in lines if len(ln) > 10 and ln[4] == "-" and ln[7] == "-"]
        if not dated:
            return []
        return dated

    def _errors_today(self, limit: int = 300) -> dict:
        """当日（日志里最新那天）的报错清单，带多行堆栈。"""
        today = time.strftime("%Y-%m-%d")
        path = self._log_file()
        if not path.is_file():
            return {"ok": False, "msg": f"日志文件不在：{path}", "date": today,
                    "counts": {"error": 0, "warn": 0}, "items": []}

        lines = self._tail_lines(path)
        if not lines:
            return {"ok": True, "date": today, "counts": {"error": 0, "warn": 0}, "items": [],
                    "fallback": False}

        # 今天还没写日志（或时区错位）时，退回家日志里最新那天，免得面板空着骗人
        newest = lines[-1][:10]
        want = today if any(ln.startswith(today) for ln in lines) else newest
        fallback = want != today

        items = []
        err = warn = 0
        for ln in lines:
            if not ln.startswith(want):
                continue
            m = _LOG_RE.match(ln)
            if m:
                level = "ERROR" if m.group(3) in ("ERROR", "CRITICAL") else "WARN"
                if level == "ERROR":
                    err += 1
                else:
                    warn += 1
                items.append({
                    "t": m.group(2),
                    "lv": level,
                    "raw": m.group(3),
                    "src": m.group(4) or "-",
                    "msg": m.group(5)[:500],
                    "detail": [],
                })
            elif items:
                # 不以时间戳开头的行＝上一段报错的堆栈/续行
                tail = items[-1]["detail"]
                if len(tail) < 12:
                    tail.append(ln[:300])

        # 归并：同一类报错反复刷屏时，面板该看到「几次」而不是「几条」。
        # NapCat 重连失败那种一分钟几十条的情况，不归并根本没法看。
        bucket = {}
        by_hour = {}
        for it in items:
            key = _err_key(it.get("src") or "", it.get("msg") or "")
            g = bucket.get(key)
            if g is None:
                g = {
                    "key": key[:40],
                    "n": 0,
                    "lv": it["lv"],
                    "src": it.get("src") or "-",
                    "head": _err_head(it.get("msg") or ""),
                    "first": it.get("t"),
                    "last": it.get("t"),
                }
                bucket[key] = g
            g["n"] += 1
            g["last"] = it.get("t")
            if it["lv"] == "ERROR":
                g["lv"] = "ERROR"
            hour = str(it.get("t") or "")[:2]
            if len(hour) == 2 and hour.isdigit():
                by_hour[hour] = by_hour.get(hour, 0) + 1

        groups = sorted(bucket.values(), key=lambda x: x["n"], reverse=True)

        kept = list(reversed(items[-limit:]))
        return {
            "ok": True,
            "date": want,
            "fallback": fallback,
            "counts": {"error": err, "warn": warn},
            "shown": len(kept),
            "truncated": len(items) > limit,
            "groups": groups[:80],
            "by_hour": by_hour,
            "items": kept,
        }

    def _host_row(self) -> dict:
        try:
            import psutil
        except Exception:
            return {"ok": False, "msg": "psutil 不可用"}

        try:
            proc = psutil.Process(os.getpid())
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage(str(Path(__file__).anchor or "/"))
            return {
                "ok": True,
                "cpu": round(psutil.cpu_percent(interval=None), 1),
                "cores": psutil.cpu_count(logical=True) or 0,
                "mem": round(mem.percent, 1),
                "mem_used": round((mem.total - mem.available) / (1024 ** 3), 1),
                "mem_total": round(mem.total / (1024 ** 3), 1),
                "disk": round(disk.percent, 1),
                "boot_s": int(time.time() - psutil.boot_time()),
                "proc_s": int(time.time() - proc.create_time()),
                "proc_mem": round(proc.memory_info().rss / (1024 ** 2), 1),
            }
        except Exception as exc:
            logger.error("[550C] 采集主机状态失败：%s", exc)
            return {"ok": False, "msg": str(exc)}

    # ---- 采样：把每次轮询的现值落盘，才有趋势可画 ------------------
    # 之前面板只有「当前值」，磁盘 91% 这种数字没有曲线就看不出还有多久满，
    # 内存涨到什么程度、token 什么时候冲起来也全都看不见。
    # 每次 /status 采一条，20 秒一条，24h 约 4320 条，单条不到 100 字节。
    _SAMPLE_NAME = "samples.jsonl"
    _SAMPLE_KEEP = 4320        # 24h @ 20s
    _SAMPLE_MIN_GAP = 15       # 两个标签页同时轮询时别重复记

    def _sample_path(self) -> Path:
        return Path(__file__).parent / self._SAMPLE_NAME

    def _append_sample(self, status: dict) -> None:
        """追加一条采样。任何异常都吞掉——采样不该影响状态接口。"""
        now = int(time.time())
        if now - self._last_sample_at < self._SAMPLE_MIN_GAP:
            return
        host = status.get("host") or {}
        tokens = status.get("tokens") or {}
        row = {
            "t": now,
            "cpu": host.get("cpu"),
            "mem": host.get("mem"),
            "disk": host.get("disk"),
            "tok": tokens.get("total") if isinstance(tokens, dict) else None,
        }
        try:
            path = self._sample_path()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._last_sample_at = now
        except Exception as exc:
            logger.debug("[550C] 采样写入失败：%s", exc)
            return
        try:
            if path.stat().st_size > self._SAMPLE_KEEP * 160:
                self._trim_samples(path)
        except Exception:
            pass

    def _trim_samples(self, path: Path) -> None:
        """超量时只保留最近 _SAMPLE_KEEP 行，重写而不是删文件。"""
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if len(lines) <= self._SAMPLE_KEEP:
                return
            path.write_text("\n".join(lines[-self._SAMPLE_KEEP:]) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.debug("[550C] 采样裁剪失败：%s", exc)

    def _load_samples(self, hours: int = 24) -> list:
        """读回最近 N 小时的采样点，坏行跳过。"""
        path = self._sample_path()
        if not path.is_file():
            return []
        cutoff = int(time.time()) - max(1, hours) * 3600
        out = []
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            logger.error("[550C] 采样读取失败：%s", exc)
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if int(row.get("t") or 0) >= cutoff:
                out.append(row)
        return out

    # ---- 阈值告警 ---------------------------------------------------
    _ALERT_COOL = 600   # 同一条告警 10 分钟内不重复推

    def _evaluate_alerts(self, status: dict) -> list:
        """按阈值把当前状态折成告警清单。前端拿它画横幅，webhook 拿它推消息。"""
        cfg = self.plugin_cfg or {}
        if not bool(cfg.get("alert_enabled", DEFAULTS["alert_enabled"])):
            return []

        out = []
        host = status.get("host") or {}
        if host.get("ok"):
            for key, label, opt in (
                ("disk", "磁盘", "alert_disk"),
                ("mem", "内存", "alert_mem"),
                ("cpu", "CPU", "alert_cpu"),
            ):
                pct = host.get(key)
                if pct is None:
                    continue
                try:
                    pct = float(pct)
                    limit = float(cfg.get(opt) or DEFAULTS[opt])
                except Exception:
                    continue
                if pct < limit:
                    continue
                # 贴着阈值是 warn，超过阈值 5 个点以上算 bad
                lv = "bad" if pct >= min(99.5, limit + 5) else "warn"
                out.append({
                    "id": f"host-{key}",
                    "lv": lv,
                    "title": f"{label} {pct:.1f}%",
                    "detail": f"已超过阈值 {limit:g}%",
                })

        # 链路：配置里开着、却拿不到连接，就是断了。这正是 NapCat 反复重连的那类问题。
        for row in status.get("adapters") or []:
            if not row.get("enabled") or not row.get("live"):
                continue
            name = row.get("name") or row.get("id") or "-"
            if row.get("dead"):
                out.append({
                    "id": f"link-dead-{row.get('id')}",
                    "lv": "bad",
                    "title": f"{name} 已自我保护停机",
                    "detail": "适配器判定永久断开，需要回看前一条真根因",
                })
            elif row.get("socket") is False:
                out.append({
                    "id": f"link-socket-{row.get('id')}",
                    "lv": "bad",
                    "title": f"{name} WebSocket 未连接",
                    "detail": "取表情 / 引用消息 / 翻历史会连带失败，恢复后整片消失",
                })
            elif row.get("logged_in") is False:
                out.append({
                    "id": f"link-login-{row.get('id')}",
                    "lv": "warn",
                    "title": f"{name} 已连接但未登录",
                    "detail": "socket 在，登录事件没等到，消息可能发不出去",
                })
        return out

    async def _notify_webhook(self, alerts: list) -> None:
        """把新增告警 POST 到配置里的 webhook。没配就什么都不做。"""
        url = str((self.plugin_cfg or {}).get("alert_webhook") or "").strip()
        if not url.startswith(("http://", "https://")) or not alerts:
            return

        now = time.time()
        fresh = []
        for item in alerts:
            key = str(item.get("id") or "")
            if now - float(self._alert_sent.get(key) or 0) < self._ALERT_COOL:
                continue
            fresh.append(item)
        if not fresh:
            return

        payload = json.dumps(
            {"source": "550C 操作台", "ts": int(now), "alerts": fresh},
            ensure_ascii=False,
        ).encode("utf-8")

        def _post() -> int:
            import urllib.request

            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json; charset=utf-8"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return int(getattr(resp, "status", 0) or 0)

        code = await asyncio.to_thread(_post)
        if 200 <= code < 300:
            for item in fresh:
                self._alert_sent[str(item.get("id") or "")] = now
            logger.info("[550C] 已推送 %d 条告警", len(fresh))

    async def _collect_status(self) -> dict:
        return {
            "ts": int(time.time()),
            "version": self._version(),
            "adapters": self._adapter_rows(),
            "models": self._model_rows(),
            "tokens": await self._token_row(),
            "host": self._host_row(),
            "plugins": await asyncio.to_thread(self._plugin_rows),
            # 自动更新那块要读插件清单，也丢线程里
            "updater": (await asyncio.to_thread(self.updater.status)
                        if getattr(self, "updater", None) is not None else None),
        }

    def _version(self) -> str:
        try:
            import json

            data = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
            return str(data.get("version") or "-")
        except Exception:
            return "-"

    async def initialize(self):
        if not (self.web_dir / "show.html").is_file():
            logger.error(
                "[550C] 缺少 web/show.html —— 先执行 "
                "python3 scripts/gen.py assets/550C-source.html web"
            )
        else:
            logger.info("[550C] 开机动画已就绪，面板配置：%s", self._settings())

        # 自动更新：一个后台协程，到点自己跑，不吵人
        try:
            self.updater = AutoUpdater(self)
            await self.updater.start()
        except Exception as exc:
            logger.error("[550C] 自动更新没挂起来：%s", exc)
            self.updater = None

    async def terminate(self):
        if self.updater is not None:
            try:
                await self.updater.stop()
            except Exception as exc:
                logger.warning("[550C] 自动更新收尾失败：%s", exc)
            self.updater = None
        logger.info("[550C] 控制台已卸载")

    def _settings(self) -> dict:
        cfg = self.plugin_cfg or {}
        mode = str(cfg.get("mode") or DEFAULTS["mode"]).lower()
        scheme = str(cfg.get("scheme") or DEFAULTS["scheme"]).lower()
        if mode not in MODE_OPTIONS:
            mode = DEFAULTS["mode"]
        if scheme not in SCHEME_OPTIONS:
            scheme = DEFAULTS["scheme"]
        return {
            "mode": mode,
            "scheme": scheme,
            "auto": bool(cfg.get("auto_play", DEFAULTS["auto_play"])),
            "loop": bool(cfg.get("loop", DEFAULTS["loop"])),
            "duration": int(cfg.get("duration_hint") or DEFAULTS["duration_hint"]),
        }

    @register.page(
        "/console",
        menu=PageMenu(
            label={"zh": "550C 开机", "en": "550C Boot"},
            icon="Monitor",
            order=60,
        ),
    )
    def console(self):
        return PluginPage.from_folder("./web")

    @register.api("GET", "/errors")
    async def api_errors(self):
        # 读文件是同步的，丢线程里做，别卡住消息处理
        data = await asyncio.to_thread(self._errors_today)
        return {"status": "ok", "data": data}

    @register.api("GET", "/status")
    async def api_status(self):
        data = await self._collect_status()
        # 采样落盘：趋势要历史，光有现值画不出曲线
        await asyncio.to_thread(self._append_sample, data)
        alerts = self._evaluate_alerts(data)
        data["alerts"] = alerts
        if alerts:
            try:
                await self._notify_webhook(alerts)
            except Exception as exc:
                logger.warning("[550C] 告警推送失败：%s", exc)
        return {"status": "ok", "data": data}

    @register.api("GET", "/history")
    async def api_history(self):
        """趋势用：返回最近若干小时的采样点（小时数由 trend_hours 配置决定）。"""
        span = int((self.plugin_cfg or {}).get("trend_hours") or DEFAULTS["trend_hours"])
        points = await asyncio.to_thread(self._load_samples, span)
        return {"status": "ok", "data": {"hours": span, "count": len(points), "points": points}}

    @register.api("GET", "/report")
    async def api_report(self):
        """导出日报（Markdown）。"""
        data = await self._collect_status()
        markdown = await asyncio.to_thread(self._build_report, data, 1)
        return {"status": "ok", "data": {"markdown": markdown, "days": 1}}

    @register.api("GET", "/report/week")
    async def api_report_week(self):
        """导出周报（Markdown，7 天趋势）。"""
        data = await self._collect_status()
        markdown = await asyncio.to_thread(self._build_report, data, 7)
        return {"status": "ok", "data": {"markdown": markdown, "days": 7}}

    @register.api("GET", "/diag")
    async def api_diag(self):
        """诊断包：把排查要用的东西一次性打包，方便整份发给别人看。

        只做只读采集，不提供「重连 / 清缓存」这类会改运行时状态的按钮——
        插件拿不到稳定的适配器控制 API，猜着调用比不给更危险。
        """
        data = await self._collect_status()
        alerts = self._evaluate_alerts(data)
        errs = await asyncio.to_thread(self._errors_today, 120)
        span = int((self.plugin_cfg or {}).get("trend_hours") or DEFAULTS["trend_hours"])
        points = await asyncio.to_thread(self._load_samples, span)
        tokens = dict(data.get("tokens") or {})
        tokens.pop("by_model", None)   # 明细太长，包里只留汇总
        return {
            "status": "ok",
            "data": {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "plugin_version": data.get("version"),
                "settings": self._settings(),
                "host": data.get("host"),
                "adapters": data.get("adapters"),
                "tokens": tokens,
                "alerts": alerts,
                "errors": {
                    "date": errs.get("date"),
                    "ok": errs.get("ok"),
                    "msg": errs.get("msg"),
                    "counts": errs.get("counts"),
                    "groups": (errs.get("groups") or [])[:30],
                    "by_hour": errs.get("by_hour"),
                },
                "trend": {
                    "hours": span,
                    "count": len(points),
                    "points": [
                        {"t": p.get("t"), "cpu": p.get("cpu"), "mem": p.get("mem"), "disk": p.get("disk")}
                        for p in points[-1200:]
                    ],
                },
            },
        }

    def _build_report(self, status: dict, days: int = 1) -> str:
        """把当前快照 + 采样趋势拼成一份能直接贴进聊天的 Markdown。"""
        span = 1 if int(days or 1) <= 1 else 7
        label = "日报" if span == 1 else "周报"
        out = []
        out.append(f"# 550C 运行{label}")
        out.append("")
        out.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        out.append(f"插件版本：v{status.get('version') or '-'}")
        out.append("")

        host = status.get("host") or {}
        if host.get("ok"):
            up_h = int(int(host.get("boot_s") or 0) // 3600)
            proc_h = int(int(host.get("proc_s") or 0) // 3600)
            out.append("## 主机")
            out.append("")
            out.append("| 项目 | 数值 |")
            out.append("| --- | --- |")
            out.append(f"| CPU | {host.get('cpu')}%（{host.get('cores')} 核）|")
            out.append(f"| 内存 | {host.get('mem')}%（{host.get('mem_used')} / {host.get('mem_total')} GB）|")
            out.append(f"| 磁盘 | {host.get('disk')}% |")
            out.append(f"| 系统已开机 | {up_h} 小时 |")
            out.append(f"| 进程已运行 | {proc_h} 小时（{host.get('proc_mem')} MB）|")
            out.append("")
        else:
            out.append(f"## 主机\n\n采集不可用：{host.get('msg') or '未知原因'}\n")

        points = self._load_samples(span * 24)
        if points:
            out.append(f"## 趋势（最近 {span * 24} 小时，{len(points)} 个采样点）")
            out.append("")
            out.append("| 指标 | 最低 | 平均 | 最高 |")
            out.append("| --- | --- | --- | --- |")
            for key, name in (("cpu", "CPU %"), ("mem", "内存 %"), ("disk", "磁盘 %")):
                vals = [
                    float(p[key]) for p in points
                    if isinstance(p.get(key), (int, float))
                ]
                if not vals:
                    out.append(f"| {name} | - | - | - |")
                    continue
                out.append(
                    f"| {name} | {min(vals):.1f} | {sum(vals) / len(vals):.1f} | {max(vals):.1f} |"
                )
            out.append("")
        else:
            out.append("## 趋势\n\n采样文件还没有数据，面板多开一会儿就会累积。\n")

        tokens = status.get("tokens") or {}
        if isinstance(tokens, dict) and tokens.get("ok"):
            # 字段名对齐 _token_row 的真实返回（input/output，不是 prompt/completion）
            out.append("## 用量（24 小时）")
            out.append("")
            out.append("| 项目 | 数值 |")
            out.append("| --- | --- |")
            out.append(f"| 调用次数 | {int(tokens.get('calls') or 0):,} |")
            out.append(f"| 输入 token | {int(tokens.get('input') or 0):,} |")
            out.append(f"| 输出 token | {int(tokens.get('output') or 0):,} |")
            cached = int(tokens.get("cached") or 0)
            if cached:
                out.append(f"| 缓存命中 | {cached:,} |")
            out.append(f"| 合计 | {int(tokens.get('total') or 0):,} |")
            avg_ms = tokens.get("avg_ms")
            if avg_ms:
                out.append(f"| 平均响应 | {avg_ms} ms |")
            cost = tokens.get("cost")
            if cost is not None:
                out.append(f"| 折算金额 | ¥{float(cost):.4f} |")
            out.append("")

            top = (tokens.get("by_model") or [])[:8]
            if top:
                out.append(f"### 按模型（前 {len(top)} 个）")
                out.append("")
                out.append("| 模型 | 调用 | 输入 | 输出 | 金额 |")
                out.append("| --- | --- | --- | --- | --- |")
                for row in top:
                    money = row.get("cost")
                    money_txt = f"¥{float(money):.4f}" if money is not None else "未配价"
                    out.append(
                        f"| {row.get('model')} | {row.get('calls')} | "
                        f"{int(row.get('input') or 0):,} | {int(row.get('output') or 0):,} | {money_txt} |"
                    )
                out.append("")
            unpriced = tokens.get("unknown_price") or []
            if unpriced:
                out.append(f"> 未配价模型：{', '.join(unpriced[:6])}（在 prices.json 里补上就能折算）")
                out.append("")
        else:
            out.append("## 用量\n\n宿主这版没有用量统计接口，跳过。\n")

        alerts = self._evaluate_alerts(status)
        out.append("## 告警")
        out.append("")
        if alerts:
            for item in alerts:
                icon = "🔴" if item.get("lv") == "bad" else "🟡"
                out.append(f"- {icon} **{item.get('title')}** —— {item.get('detail')}")
        else:
            out.append("- 无")
        out.append("")

        errs = self._errors_today(limit=50)
        if errs.get("ok"):
            groups = errs.get("groups") or []
            out.append(f"## 报错（{errs.get('date')}，共 {sum(g.get('n', 0) for g in groups)} 条 / {len(groups)} 类）")
            out.append("")
            if groups:
                out.append("| 次数 | 级别 | 来源 | 摘要 |")
                out.append("| --- | --- | --- | --- |")
                for g in groups[:10]:
                    src = g.get("src") or "-"
                    head = (g.get("head") or "").replace("|", "\\|")
                    out.append(f"| {g.get('n')} | {g.get('lv')} | {src} | {head} |")
            else:
                out.append("- 今天没有 ERROR / WARNING")
            out.append("")
        else:
            # 日志读不到时也要留一段：报告缺一块比写清楚「缺这块」更误导人
            out.append("## 报错")
            out.append("")
            out.append(f"- 日志读不到：{errs.get('msg') or '未知原因'}")
            out.append("")

        return "\n".join(out)

    @register.api("GET", "/settings")
    async def api_get_settings(self):
        return {"status": "ok", "data": self._settings()}

    @register.api("POST", "/settings")
    async def api_save_settings(self, payload: dict = None):
        payload = payload if isinstance(payload, dict) else {}
        patch = {}
        if str(payload.get("mode", "")).lower() in MODE_OPTIONS:
            patch["mode"] = str(payload["mode"]).lower()
        if str(payload.get("scheme", "")).lower() in SCHEME_OPTIONS:
            patch["scheme"] = str(payload["scheme"]).lower()
        for key in ("auto_play", "loop"):
            if key in payload:
                patch[key] = bool(payload[key])
        if "duration_hint" in payload:
            try:
                patch["duration_hint"] = max(6, min(30, int(payload["duration_hint"])))
            except (TypeError, ValueError):
                pass
        if not patch:
            return {"status": "error", "msg": "没有可保存的字段"}

        mgr = getattr(self.ctx, "plugin_mgr", None)
        if mgr is None or not hasattr(mgr, "update_plugin_config"):
            return {"status": "error", "msg": "当前环境不支持写入插件配置"}

        try:
            await mgr.update_plugin_config(PLUGIN_ID, patch)
        except Exception as exc:  # 写盘失败不该让面板炸掉
            logger.error("[550C] 保存首选项失败：%s", exc)
            return {"status": "error", "msg": f"保存失败：{exc}"}

        self.plugin_cfg = getattr(self, "plugin_cfg", None) or {}
        self.plugin_cfg.update(patch)
        logger.info("[550C] 首选项已更新：%s", patch)
        return {"status": "ok", "msg": "已记住这份设置", "data": self._settings()}

    # ---- 插件点灯：本地清单 × 市场版本 ----------------
    @staticmethod
    def _data_root() -> Path:
        """数据目录。优先问核心，问不到就按插件所在位置倒推。"""
        try:
            from core.utils.path_utils import get_data_path

            return Path(get_data_path())
        except Exception:
            return Path(__file__).resolve().parents[2]

    def _store_versions(self) -> tuple[dict, str]:
        """市场缓存里 plugin_id -> {version, repo}，取 plugin_src 下最新的那份。"""
        out: dict = {}
        newest = "-"
        try:
            import json

            src_dir = self._data_root() / "plugin_src"
            files = sorted(src_dir.glob("plugins_*.json"),
                           key=lambda f: f.stat().st_mtime, reverse=True)
        except Exception:
            return out, newest

        for f in files[:2]:
            try:
                import json
                import time as _t

                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if newest == "-":
                try:
                    newest = time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime))
                except Exception:
                    newest = "-"
            items = data.get("plugins") if isinstance(data, dict) else None
            if isinstance(items, dict):
                pairs = list(items.items())
            elif isinstance(items, list):
                pairs = [(i.get("plugin_id"), i) for i in items if isinstance(i, dict)]
            else:
                pairs = []
            for pid, item in pairs:
                if not pid or not isinstance(item, dict):
                    continue
                ver = str(item.get("version") or "").strip()
                if ver and str(pid) not in out:
                    out[str(pid)] = {"version": ver, "repo": str(item.get("repo") or "")}
        return out, newest

    def _plugin_rows(self) -> dict:
        """每个插件一盏灯：红=加载报错，黄=没加载完，蓝=有新版本，灰=没来源，绿=最新。"""
        mgr = getattr(self.ctx, "plugin_mgr", None)
        if mgr is None:
            return {"ts": int(time.time()), "market_at": "-", "rows": [], "counts": {},
                    "error": "当前环境读不到插件管理"}
        store, market_at = self._store_versions()
        try:
            errors = mgr.get_plugin_load_errors() or {}
        except Exception:
            errors = {}

        rows, counts = [], {"green": 0, "blue": 0, "amber": 0, "red": 0, "grey": 0}
        for info in mgr.list_plugins():
            if getattr(info, "builtin", False) or getattr(info, "hidden", False):
                continue
            pid = str(info.plugin_id)
            ver = str(info.version or "") or "-"
            status = str(getattr(info, "status", "") or "")
            s = store.get(pid)
            light, note, latest = "grey", "市场未收录", ""

            if status == "error" or pid in errors:
                light, note = "red", "加载报错"
                err = errors.get(pid) or {}
                note = f"加载报错：{str(err.get('error') or '')[:60]}" if err else note
            elif status in ("pending", "loading", "installing"):
                light, note = "amber", {"pending": "等待中", "loading": "加载中",
                                        "installing": "正在装依赖"}.get(status, status)
            elif s:
                latest = s["version"]
                try:
                    from packaging.version import Version

                    newer = Version(latest.lstrip("v")) > Version(ver.lstrip("v"))
                except Exception:
                    newer = latest.lstrip("v") != ver.lstrip("v")
                if newer:
                    light, note = "blue", f"有新版 {latest}"
                else:
                    light, note = "green", "已最新"
            else:
                # 市场里没有这份来源：能跑就说清楚是「自装、无来源」，别冒充绿灯
                light = "grey" if status == "ready" else "amber"
                note = "运行中 · 市场未收录" if status == "ready" else (status or "未知")

            rows.append({
                "id": pid,
                "name": str(getattr(info, "display_name", "") or pid),
                "version": ver,
                "latest": latest,
                "light": light,
                "note": note,
                "enabled": bool(mgr.is_plugin_enabled(pid)) if hasattr(mgr, "is_plugin_enabled") else True,
            })
            counts[light] = counts.get(light, 0) + 1

        rows.sort(key=lambda r: ({"blue": 0, "red": 1, "amber": 2, "grey": 3, "green": 4}[r["light"]],
                                 r["name"].lower()))
        return {"ts": int(time.time()), "market_at": market_at, "rows": rows,
                "counts": counts, "total": len(rows)}

    @register.api("GET", "/plugins")
    async def api_plugins(self):
        # 读市场缓存是磁盘活，丢线程里，别卡消息处理
        return {"status": "ok", "data": await asyncio.to_thread(self._plugin_rows)}

    # ---- 插件自动更新：状态 / 立即检查 / 设置 ----
    @register.api("GET", "/updater")
    async def api_updater(self):
        if self.updater is None:
            return {"status": "error", "msg": "自动更新没挂起来，看日志里的 550C 记录"}
        # 算待更新清单要读插件清单，丢线程里
        return {"status": "ok", "data": await asyncio.to_thread(self.updater.status)}

    @register.api("POST", "/updater/check")
    async def api_updater_check(self, payload: dict = None):
        if self.updater is None:
            return {"status": "error", "msg": "自动更新没挂起来"}
        if self.updater.status().get("busy"):
            return {"status": "ok", "msg": "正在查，别急"}

        async def _run():
            result = await self.updater.check_now()
            logger.info("[550C] 手动检查结束：%s", result)

        # 下载可能要几十秒，别把面板的请求挂着；面板自己看 busy 和最近结果
        asyncio.create_task(_run(), name="550c-manual-check")
        return {"status": "ok", "msg": "已开始检查"}

    @register.api("POST", "/updater/settings")
    async def api_updater_settings(self, payload: dict = None):
        if self.updater is None:
            return {"status": "error", "msg": "自动更新没挂起来"}
        payload = payload if isinstance(payload, dict) else {}
        patch = {}
        if "auto_update" in payload:
            patch["auto_update"] = bool(payload["auto_update"])
        for key, lo, hi in (("update_interval_hours", 1, 168), ("update_max_per_run", 1, 10)):
            if key in payload:
                try:
                    patch[key] = max(lo, min(hi, int(payload[key])))
                except (TypeError, ValueError):
                    pass
        if "update_exclude" in payload:
            patch["update_exclude"] = str(payload["update_exclude"] or "").strip()
        if not patch:
            return {"status": "error", "msg": "没有可保存的字段"}

        mgr = getattr(self.ctx, "plugin_mgr", None)
        if mgr is None or not hasattr(mgr, "update_plugin_config"):
            return {"status": "error", "msg": "当前环境不支持写入插件配置"}
        try:
            await mgr.update_plugin_config(PLUGIN_ID, patch)
        except Exception as exc:
            logger.error("[550C] 保存自动更新设置失败：%s", exc)
            return {"status": "error", "msg": "保存失败：%s" % exc}

        self.plugin_cfg = getattr(self, "plugin_cfg", None) or {}
        self.plugin_cfg.update(patch)
        # 间隔变了就重排下次，免得还是按旧周期等
        if "update_interval_hours" in patch:
            self.updater.reset_schedule()
        logger.info("[550C] 自动更新设置已更新：%s", patch)
        return {"status": "ok", "msg": "已记住", "data": await asyncio.to_thread(self.updater.status)}

    @register.api("POST", "/updater/unblock")
    async def api_updater_unblock(self, payload: dict = None):
        if self.updater is None:
            return {"status": "error", "msg": "自动更新没挂起来"}
        payload = payload if isinstance(payload, dict) else {}
        pid = str(payload.get("id") or "").strip()
        if not pid:
            return {"status": "error", "msg": "没说是哪个插件"}
        self.updater.unblock(pid)
        return {"status": "ok", "msg": "已放出来，下轮会重试一次",
                "data": await asyncio.to_thread(self.updater.status)}
