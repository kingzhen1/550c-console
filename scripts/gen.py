#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Voidpoket 的 550C 原稿改写成 KiraAI 面板可以嵌的播放页。

动画不做重写，只做定点改写（与 dsh-550c-boot 同一套做法）：
  * 样式表 / DOM 结构 / 动画脚本逐字搬运
  * 去掉页面级自动开机与键盘、点击监听，播放时机交给外层控制台
  * `rgba(232,160,32,α)` 全部收口成 `rgba(var(--amber-rgb),α)`，配色只换三元组
  * 追加待机层（未自动播放时显示）

用法: python3 gen.py <550C-source.html> <输出目录>
"""
import re
import sys
from pathlib import Path

# 配色只声明基础量，各半透明派生量都从 --amber-rgb 算
SCHEMES = {
    "green": {
        "--amber": "#7ee06a", "--amber-b": "#adff8c", "--amber-d": "#2f5c22",
        "--amber-rgb": "126,224,106",
        "--text": "#b9dcae", "--text-dim": "#7a9c6c", "--text-faint": "#41603a",
        "--bg": "#030503", "--bg-panel": "#061006", "--bg-win": "#081408",
        "--bg-hud-a": "#081408", "--bg-hud-b": "#030803",
    },
    "cyan": {
        "--amber": "#4fd1c5", "--amber-b": "#8ff3ea", "--amber-d": "#1d5a55",
        "--amber-rgb": "79,209,197",
        "--text": "#a6ded9", "--text-dim": "#6a938f", "--text-faint": "#2f4c4a",
        "--bg": "#030607", "--bg-panel": "#061113", "--bg-win": "#081518",
        "--bg-hud-a": "#081518", "--bg-hud-b": "#030809",
    },
    "white": {
        "--amber": "#d6d6d6", "--amber-b": "#ffffff", "--amber-d": "#575757",
        "--amber-rgb": "214,214,214",
        "--text": "#c8c8c8", "--text-dim": "#8c8c8c", "--text-faint": "#4a4a4a",
        "--bg": "#040404", "--bg-panel": "#0b0b0b", "--bg-win": "#101010",
        "--bg-hud-a": "#101010", "--bg-hud-b": "#050505",
    },
}

STANDBY = """
<div id="standby">
  <div class="sb-mark">550C · UAV-BS-07</div>
  <div class="sb-line"></div>
  <div class="sb-pulse"></div>
  <div class="sb-text">点击开始</div>
</div>
"""

EXTRA_CSS = """

/* ===== 配色方案：默认 amber 即原稿配色，其余只换基础变量 ===== */
{schemes}

/* ===== 待机层：未自动播放时显示，点击开始 ===== */
#standby{{position:fixed;inset:0;z-index:2500;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:16px;background:var(--bg);
  cursor:pointer;font-family:"Courier New",Consolas,monospace;transition:opacity .45s ease;}}
#standby.hide{{opacity:0;pointer-events:none;}}
#standby .sb-mark{{font-size:12px;letter-spacing:.62em;padding-left:.62em;color:var(--amber);
  text-shadow:0 0 14px rgba(var(--amber-rgb),.35);text-transform:uppercase;}}
#standby .sb-line{{width:180px;height:1px;background:var(--amber-d);opacity:.7;}}
#standby .sb-text{{font-size:10px;letter-spacing:.34em;padding-left:.34em;color:var(--text-faint);
  text-transform:uppercase;}}
#standby .sb-pulse{{width:6px;height:6px;border-radius:50%;background:var(--amber);
  box-shadow:0 0 10px var(--amber);animation:sbPulse 1.6s ease-in-out infinite;}}
@keyframes sbPulse{{0%,100%{{opacity:.25}}50%{{opacity:1}}}}
"""

CONTROL = """
<script>
/* ===== 控制层：URL 参数决定 模式/配色/是否自动播放，结束回报父窗口 ===== */
(function () {
  "use strict";
  var p = new URLSearchParams(location.search);
  var MODE = (p.get("mode") || "simple").toLowerCase();
  var SCHEME = (p.get("scheme") || "amber").toLowerCase();
  var AUTO = p.get("auto") === "1";
  if (SCHEME && SCHEME !== "amber") document.documentElement.setAttribute("data-scheme", SCHEME);
  if (MODE === "off") {
    var sb = document.getElementById("standby");
    if (sb) sb.classList.add("hide");
    try { parent.postMessage({ type: "550c:off", mode: MODE, scheme: SCHEME }, "*"); } catch (e) {}
    return;
  }

  var standby = document.getElementById("standby");
  var played = false, ended = false;

  function post(type, extra) {
    var msg = { type: "550c:" + type, mode: MODE, scheme: SCHEME };
    if (extra) for (var k in extra) msg[k] = extra[k];
    try { parent.postMessage(msg, "*"); } catch (e) {}
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function finish(reason) {
    if (ended) return;
    ended = true;
    // full 模式跑完要把 final 面板留在屏幕上；只有跳过/简化收尾才淡出
    if (reason !== "full") {
      var root = document.documentElement;
      root.style.transition = "opacity .45s ease";
      root.style.opacity = "0";
    }
    setTimeout(function () { post("done", { reason: reason }); }, 470);
  }
  function start() {
    if (played || !window.__550c) return;
    played = true;
    if (standby) standby.classList.add("hide");
    post("started");
    if (MODE === "simple") {
      wait(window.__550c.playBoot() + 300 + 900).then(function () { finish("simple"); });
      return;
    }
    var finalEl = document.getElementById("final");
    if (!finalEl) { window.__550c.boot(); return finish("full"); }
    new MutationObserver(function (obs, self) {
      if (finalEl.classList.contains("show")) {
        self.disconnect();
        setTimeout(function () { finish("full"); }, 1600);
      }
    }).observe(finalEl, { attributes: true, attributeFilter: ["class"] });
    window.__550c.boot();
  }

  window.addEventListener("message", function (ev) {
    var d = ev.data || {};
    if (d.cmd === "play") { played = false; ended = false; document.documentElement.style.opacity = ""; start(); }
    else if (d.cmd === "skip") finish("skip");
  });
  document.addEventListener("click", function () {
    if (!played) start();
    else if (!ended) finish("skip");
  }, true);

  if (AUTO) start();
  else post("ready");
})();
</script>
"""


def slice_div(source: str, marker: str) -> str:
    """按标签配平切出一整块 div（原稿里这些块嵌得很深，正则不可靠）。"""
    start = source.index(marker)
    open_end = source.index(">", start) + 1
    depth = 1
    for m in re.finditer(r"<div\b|</div>", source[open_end:]):
        depth += -1 if m.group(0) == "</div>" else 1
        if depth == 0:
            return source[start: open_end + m.end()]
    raise SystemExit("unbalanced element: " + marker)


def build(source_path: Path, out_dir: Path) -> None:
    html = source_path.read_text(encoding="utf-8")

    css = re.search(r"<style>([\s\S]*?)</style>", html).group(1)
    boot = slice_div(html, '<div id="boot">')
    app = slice_div(html, '<div id="app">')
    dim = slice_div(html, '<div id="dim">')
    final = slice_div(html, '<div id="final">')
    hint = slice_div(html, '<div id="hint">')
    js = re.search(r"<script>([\s\S]*?)</script>", html).group(1).strip()

    # ── 样式：琥珀色常量收口 ───────────────────────────────────────────
    css = css.replace("rgba(232,160,32,", "rgba(var(--amber-rgb),")
    css = css.replace("--amber:#e8a020;", "--amber:#e8a020;--amber-rgb:232,160,32;")
    css = css.replace(
        "linear-gradient(180deg,#141008,#0a0805)",
        "linear-gradient(180deg,var(--bg-hud-a),var(--bg-hud-b))",
    )
    css = css.replace(
        "linear-gradient(180deg,#161006,#0a0805)",
        "linear-gradient(180deg,var(--bg-hud-a),var(--bg-hud-b))",
    )
    naked = re.findall(r"232,160,32", css)
    if len(naked) != 1:
        print("  warn: hard-coded amber left in css: %d" % len(naked))
    for need in ("--amber-rgb:232,160,32;", "var(--bg-hud-a)"):
        if need not in css:
            print("  warn: expected token missing:", need)

    schemes = "\n".join(
        ':root[data-scheme="%s"]{%s}'
        % (name, ";".join("%s:%s" % kv for kv in variables.items()))
        for name, variables in SCHEMES.items()
    )
    css += EXTRA_CSS.format(schemes=schemes)

    # ── 脚本：摘掉页面级监听，把入口交给控制台 ─────────────────────────
    js = re.sub(r'\n*window\.addEventListener\("keydown",[\s\S]*?\n\}\);\n*', "\n", js, count=1)
    js = re.sub(
        r"\n*/\* \u81ea\u52a8\u5f00\u673a[\s\S]*?"
        r"document\.addEventListener\(\"touchstart\", bootUp, \{ passive: true \}\);\n*",
        "\n", js, count=1)
    js = js.replace('document.addEventListener("click", bootUp);\n', "")
    for gone in ("window.addEventListener", "touchstart", 'addEventListener("click", bootUp)'):
        if gone in js:
            print("  warn: page-level listener survived:", gone)

    tail = "})();"
    if not js.endswith(tail):
        raise SystemExit("unexpected IIFE tail")
    js = js[: -len(tail)] + """
/* ===== 交给外层控制台：暴露入口，页面本身不再自动开机 ===== */
window.__550c = {
  playBoot: playBoot,
  boot: bootUp,
  run: run,
  launched: function () { return launched; },
};
})();"""

    hint = hint.replace("\u2014 REFRESH TO REPLAY \u2014", "\u2014 \u70b9\u51fb\u91cd\u64ad \u2014")

    page = (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        "<title>550C 开机动画</title>\n<style>\n" + css + "\n</style>\n</head>\n<body>\n"
        + boot + "\n" + app + "\n" + dim + "\n" + final + "\n" + hint + "\n" + STANDBY
        + "\n<script>\n" + js + "\n</script>\n" + CONTROL + "</body>\n</html>\n"
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "show.html").write_text(page, encoding="utf-8")
    print("  wrote %s (%d chars)" % (out_dir / "show.html", len(page)))


if __name__ == "__main__":
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "assets/550C-source.html")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "web")
    print("build: %s -> %s" % (src, dst))
    build(src, dst)
