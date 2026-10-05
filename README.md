# 550C Boot Sequence

> 550C 无人机基站开机动画，作为 KiraAI 面板插件页接入。

把 [Voidpoket] 的 550C 单页动画接进 KiraAI 面板：动画结束后展示适配器与主机实时状态，
收起待机时保留 550C 动态台标，并可随时拉出当日报错列表框与用量/金额视图。

## 功能

- **开机动画**：`simple`（只放标识点亮）/ `full`（完整流程：自检→鉴权→链路→隧道→隔离→重写→校验→就绪→完成）/ `off`（只留黑屏待机）
- **四套配色**：`amber` 原稿琥珀 / `green` / `cyan` / `white`，只换基础色，亮度层次保持原样
- **自动播放与重播**：打开面板即播、可循环、可手动重播；音效可单独关闭
- **实时状态台**：动画结束展示 NapCat / 适配器链路、模型、插件与主机负载；收起后叠实时时钟、链路与在线时长、CPU/内存/磁盘、版本号
- **当日报错列表框**：按级别（ERROR / WARNING）筛选、展开堆栈、一键复制，并附逐条匹配的处理方案
- **用量三视图**：`概览 / 模型 / 金额`，按模型统计调用与 token，并按内置价目表折算 24h 已用金额（人民币，缓存命中单独计价，可用 `prices.json` 外挂补价）

## 安装

1. KiraAI 面板 → 插件商店 → 搜索 `550C`，或直接从 GitHub Release 安装
2. 需要 KiraAI `core_version >= 2.23.0`

手动安装：下载 Release 附件解压到 `data/plugins/kira-ai-plugin-550c-boot/`，重启 KiraAI。

## 配置

面板 → 插件 → 550C 开机动画：

| 项 | 说明 |
| --- | --- |
| `mode` | 播放模式：simple / full / off |
| `scheme` | 配色：amber / green / cyan / white |
| `auto_play` | 打开面板即自动播放 |
| `loop` | 放完自动重播 |
| `duration_hint` | 完整模式时长提示（秒） |
| `sfx` | 音效开关 |
| `github_token` 等 | 见面板内 schema 说明 |

金额视图的价目表可用插件目录下的 `prices.json` 覆盖，单位：元 / 每百万 token。

## 目录结构

```
main.py              插件后端：状态采集、报错聚合、用量与金额折算
schema.json          插件配置项定义
web/index.html       控制台页面（动画 + 状态台 + 报错池 + 用量）
web/show.html        投屏/展示页
web/audio/*          音效
assets/550C-source.html  原稿动画（只读参考）
scripts/gen.py       原稿 → 面板页的定点改写脚本
```

## 说明

动画样式与结构来自原作者原稿，本项目只做「接入 KiraAI 面板」的定点改写：
去掉页面级自动开机与键盘/点击监听，播放时机交给外层控制台；颜色收口为 CSS 变量以支持换色。

## License

MIT © 2026 硳磲
