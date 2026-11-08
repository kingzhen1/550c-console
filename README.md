# 550C 操作台（550C Console）

> 550C 无人机基站开机动画 + 实时状态操作台，作为 KiraAI 面板插件页接入。

仓库：https://github.com/kingzhen1/550c-console

把 550C 单页动画接进 KiraAI 面板：动画结束后展示适配器与主机实时状态，
收起待机时保留 550C 动态台标，并可随时拉出当日报错列表框与用量/金额视图。

## 原作者与致谢

- **原版作者（全部署名权归其所有）**：哔哩哔哩 **@图寻患者**、**@某人在此处**
- 原版 550C 操作台面向 DeepSeek Harness（dsh）环境，直接装进 KiraAI 无法使用
- 本仓库是在原作者设计思路上做的 **KiraAI 适配与扩展版**（面板插件页、状态接口、报错方案库、用量统计等），并非原创
- 如原作者认为不妥，可随时联系下架

## 功能

- **开机动画**：`simple`（只放标识点亮）/ `full`（完整流程：自检→鉴权→链路→隧道→隔离→重写→校验→就绪→完成）/ `off`（只留黑屏待机）
- **四套配色**：`amber` 原稿琥珀 / `green` / `cyan` / `white`，只换基础色，亮度层次保持原样
- **自动播放与重播**：打开面板即播、可循环、可手动重播；音效可单独关闭
- **实时状态台**：动画结束展示 NapCat / 适配器链路、模型、插件与主机负载；收起后叠实时时钟、链路与在线时长、CPU/内存/磁盘、版本号
- **当日报错列表框**：按级别（ERROR / WARNING）筛选、展开堆栈、一键复制，并附逐条匹配的处理方案
- **用量三视图**：`概览 / 模型 / 金额`，按模型统计调用与 token，并按内置价目表折算 24h 已用金额（人民币，缓存命中单独计价，可用 `prices.json` 外挂补价）
- **趋势折线**（v1.7.0）：每次轮询把 CPU / 内存 / 磁盘 / token 采样落盘，状态面板底部画三条 canvas 折线，磁盘这类「缓慢爬升」的指标才看得出还有多久满
- **阈值告警**（v1.7.0）：内存 / 磁盘 / CPU 越线，或适配器 `dead` / ws 断开 / 已连接未登录时，面板顶部亮横幅；同一告警 10 分钟内只推一次，配了 `alert_webhook` 还会 POST JSON 出去
- **日报 / 周报导出**（v1.7.0）：状态面板右上两个按钮，把当前主机、趋势极值、24h 用量（含按模型明细与未配价提示）、当前告警、报错归并表拼成一份 Markdown 直接下载
- **诊断包**（v1.7.0）：把当前状态、适配器链路、告警、报错归并、趋势采样打成一个 JSON 整份下载，排查时直接发这一份
- **投屏页接地**（v1.7.0）：`show.html` 的 UPLINK / NODES / SESSION 原来是硬编码演示值，现在接收面板 postMessage 的真实状态

## 更新记录

### v1.7.1

- **描述精简**：manifest 的 `description` 与 zh/en locale 文案压到一两句，功能细节统一放本 README，插件商店列表不再被长文案撑爆。
- 无功能改动，仅版本号与文案；1.7.0 的配置、接口、页面结构完全兼容。

### v1.7.0

- **采样与趋势**：新增 `samples.jsonl`（插件目录下），每次 `/status` 采样一点（20 秒一条、最多保留 4320 条、超出重写裁剪），新增 `GET /history` 返回序列；前端在状态面板底部用 canvas 画磁盘 / 内存 / CPU 三条折线，点多时自动抽稀到 ≤360 点，`devicePixelRatio` 对齐避免发糊。
- **阈值告警**：新增 `alert_enabled` / `alert_disk` / `alert_mem` / `alert_cpu` 配置；`/status` 返回值新增 `alerts` 数组；适配器侧会区分 `dead`（自我保护停机）、`socket === false`（ws 断开）、`logged_in === false`（连着但没登录）三种状态分别告警。告警按 id 做 10 分钟冷却，避免每次都推。
- **Webhook**：在插件配置里（非面板 UI）填 `alert_webhook` 为 http(s) 地址即可，新告警以 `{source, ts, alerts:[{id,lv,title,detail}]}` POST 过去，5 秒超时，失败只记 warn 不影响接口返回。
- **报告导出**：新增 `GET /report`（日报）与 `GET /report/week`（周报），返回 Markdown 文本；前端两个按钮走 Blob 下载，下载被拦时退回复制到剪贴板。
- **报错接口增强**：`/errors` 返回值新增 `groups`（用噪声归一化后归并计数，时间戳/IP/数字串都会被抹平，所以「重连失败 59 次」和「42 次」会合成一类）与 `by_hour`（按小时分布）。原 `items` 结构完全不变，老调用方不受影响。
- **诊断包**：新增 `GET /diag`，只读采集，输出可直接外发的 JSON（含 settings / host / adapters / tokens 汇总 / alerts / errors 归并 / trend 采样，采样点截到最近 1200 个）。
- **投屏页真实状态**：`index.html` 在每次 `/status` 后向 iframe `postMessage` 一份折算过的状态；`show.html` 收到后把 UPLINK 显示成 `STABLE / DEGRADED / SEVERED`、NODES 显示成「在线 / 总数」、SESSION 显示开机时长。iframe 因换模式/配色/重播而重载时自动重喂一次。RSRP / SINR / BLER 仍保留原有的随机演示（那是指标氛围，换成 CPU 内存反而单位对不上）。
- **没有做「一键处置」**：面板上不放「重连 ws / 清缓存」这类按钮。插件拿不到稳定的适配器控制 API，猜着调用比不给更危险；现在能做到的是把它们变成看得见、会告警的指标，外加一键导出诊断包。
- **离线自测**：新增 `selftest.py`，用假 `core.plugin` 桩把插件导入进来，实际跑一遍告警评估、报告生成、采样写入/节流/时间窗过滤，共 43 项断言。改完逻辑先跑它，不用装到 KiraAI 上才能发现字段名对不上。
- 全部为增量改动：旧配置项、旧接口、旧页面结构保持兼容。版本号 1.6.2 → 1.7.0。

### v1.6.1

- 报错方案库补录三条最常见的报错，逐条给出「为什么」和「怎么办」：
  - `Tool msg not implemented`：模型自己把消息标签当工具发了，属自身书写失误，重发即可，不用动任何配置；
  - `/proc/net/dev` 不可读（取不到网络 IO）：进程 capability 被剥的环境限制，建议该栏优雅降级并压低日志级别；
  - `NapCat WebSocket 未连接`：链路断开，取自定义表情 / 引用消息 / 拉历史会连带一起失败，ws 恢复后整片消失。
- 仅补方案与文案，版本号 1.6.0 → 1.6.1，功能无破坏性改动。

### v1.6.0

- 首个商店版本：开机动画 + 实时状态台 + 当日报错池 + 用量/金额三视图。

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
| `alert_enabled` | 阈值告警总开关（v1.7.0） |
| `alert_disk` / `alert_mem` / `alert_cpu` | 三条告警阈值，默认 90 / 90 / 95（v1.7.0） |
| `trend_hours` | 趋势折线画多长时间，默认 24 小时（v1.7.0） |
| `sfx` | 音效开关 |
| `github_token` 等 | 见面板内 schema 说明 |

金额视图的价目表可用插件目录下的 `prices.json` 覆盖，单位：元 / 每百万 token。

**Webhook（v1.7.0）**：`alert_webhook` 没做进面板 schema（避免未知字段类型让配置校验整体失败），需要时直接写进插件配置文件，值填 `http(s)://…`。触发时 POST：

```json
{"source": "550C 操作台", "ts": 1793500000,
 "alerts": [{"id": "host-disk", "lv": "bad", "title": "磁盘 97.2%", "detail": "已超过阈值 90%"}]}
```

同一 `id` 在 10 分钟内只推一次；请求超时 5 秒，失败只记一条 warn，不影响面板接口。

## 目录结构

```
main.py              插件后端：状态采集、报错聚合、用量与金额折算、采样与告警
schema.json          插件配置项定义
web/index.html       控制台页面（动画 + 状态台 + 趋势折线 + 报错池 + 用量 + 导出）
web/show.html        投屏/展示页
web/audio/*          音效
assets/550C-source.html  原稿动画（只读参考）
scripts/gen.py       原稿 → 面板页的定点改写脚本
selftest.py          离线自测（假 core.plugin 桩，跑告警/报告/采样，43 项断言）
samples.jsonl        运行时生成：趋势采样点，可随时删，删了曲线从头累积
```

改完后端逻辑先跑 `python3 selftest.py`，不用装到 KiraAI 上也能发现字段名对不上这类问题。

## 说明

动画样式与结构来自原作者原稿，本项目只做「接入 KiraAI 面板」的定点改写：
去掉页面级自动开机与键盘/点击监听，播放时机交给外层控制台；颜色收口为 CSS 变量以支持换色。

## License

MIT © 2026 硳磲
