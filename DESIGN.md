# astrbot_plugin_bilibilidownload 设计文档

> 状态：设计稿（仅文档，代码未开工）
> 目标读者：作者本人（wjn1121）
> 编写日期：2026-09-13
> 参考基线：AstrBot 官方插件开发文档 + 插件市场同类插件 + 对 B 站 API 与 ebilibili 下载站的实测抓包

---

## 1. 一句话目标

群成员往群里丢一个 B 站视频链接，机器人自动完成三件事：**解析视频信息 → 用 AI 简单总结内容 → 从 ebilibili 下载视频文件并回传到群里**。

## 2. 需求拆解

| 编号 | 需求 | 说明 | 默认 |
|------|------|------|------|
| FR-1 | 链接识别 | 识别消息中的 `BV号`、`av号`、`bilibili.com/video/...` 完整链接、`b23.tv` 短链、分P参数 `?p=N` | 开 |
| FR-2 | 视频解析 | 取标题、UP主、时长、封面、简介、分区、播放/点赞/弹幕数、分P列表 | 开 |
| FR-3 | AI 总结 | 用 AstrBot 当前会话的 LLM 生成一段简短总结（默认 3~5 句 / ≤200 字） | 开 |
| FR-4 | 下载视频 | 走 `https://www.ebilibili.com/download` 的能力拿到直链并下载到本地 | 开 |
| FR-5 | 发送视频 | 以 `Comp.Video.fromFileSystem()` 把视频发回会话 | 开 |
| FR-6 | 降级 | 下载失败/超限时，仍发送解析卡片 + AI 总结 + ebilibili 网页链接 | 开 |
| FR-7 | 防滥用 | 单用户限流、单文件体积上限、单次消息只处理第一个链接、临时文件清理 | 开 |

**非目标（本期不做）**：UP 主动态订阅、番剧/影视解析、直播录制、评论区情感分析、多平台（抖音/快手/小红书）解析。这些留给后续版本，避免首版范围失控。

## 3. 与插件市场同类插件的差异

先看市场现状（据 GitHub 检索）：

| 插件 | 能力 | 与本插件的关系 |
|------|------|----------------|
| [Soulter/astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili) | BV 号解析、UP 动态订阅、番剧推荐、QQ 小程序解析 | 解析部分可参考；**不发视频文件**，且需要 `sessdata` |
| [Arturia169/astrbot_plugin_Bilibili-Full-Featured](https://github.com/Arturia169/astrbot_plugin_Bilibili-Full-Featured) | 视频/用户/直播/评论查询 + AI 总结（`/bsummary`，基于简介与热评） | 印证了「无 cookie 时用元数据+热评做总结」这条降级路线的可行性 |
| [SodaCodeSave/astrbot_plugin_biliread](https://github.com/SodaCodeSave/astrbot_plugin_biliread) | 让 AI 理解 B 站视频并自然回复 | 面向"AI 回复"而非"发文件" |
| [Echoshuo/astrbot_plugin_video_parser](https://github.com/Echoshuo/astrbot_plugin_video_parser) | 多平台视频解析 | 定位相近，但走的是通用解析而非固定下载站 |
| [chufeng/astrbot_plugin_bili_resolver](https://github.com/chufeng/astrbot_plugin_bili_resolver) | B 站链接解析 | 定位相近 |

**本插件的差异点**：把「解析 + AI 总结 + 真·下载成文件并发到群里」串成一条默认全自动的链路，且**不强制要求用户提供 B 站账号 Cookie**（无 Cookie 也能出解析与总结，只是总结质量降级）。

## 4. 外部依赖事实核查（已实测，2026-09-13）

> 这一节是编码时的接口契约，全部经真实请求验证，不是推测。

### 4.1 B 站视频信息 API

```
GET https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7
Referer: https://www.bilibili.com/
```

实测结果：`200`，`code=0`，**无需 Cookie、无需登录**。关键字段：

```
data.bvid / data.aid          # 视频 ID
data.cid                      # 默认分P的 cid（下载直链必需）
data.title                    # 标题
data.desc                     # 简介
data.duration                 # 总时长（秒）
data.pic                      # 封面 URL
data.owner.name               # UP 主
data.pages[] {cid, part, duration}   # 分P列表
data.stat {view, danmaku, reply, favorite, coin, like}  # 统计
```

### 4.2 B 站字幕 API（AI 总结的关键约束）

```
GET https://api.bilibili.com/x/player/v2?bvid=...&cid=...
GET https://api.bilibili.com/x/player/wbi/v2?bvid=...&cid=...
```

实测：两个接口都返回 `code=0`，但 **`data.subtitle.subtitles` 为空数组**（未携带 `SESSDATA` Cookie）。

**结论**：字幕（含 AI 字幕）需要登录态才能取到。因此：
- **默认路径不依赖字幕**，AI 总结基于元数据 + 热门评论（与 Full-Featured 插件同思路）。
- **可选路径**：用户配置 `sessdata` 后，尝试拉取字幕文本交给 LLM，总结质量显著提升（"真·内容总结"）。该路径需要 wbi 签名，属二期，本期先留出接口位。
- 字幕地址形如 `data.subtitle.subtitles[].subtitle_url`（JSON 数组，字段 `from/to/content`），直接转纯文本喂 LLM。

### 4.3 ebilibili 下载站（核心）

站点自述用法（首页原文）：**把 `bilibili.com` 改成 `ebilibili.com` 即可下载对应视频。**

实测可用的三条路径：

#### 路径 A（首选，最干净）：直链 API

```
GET https://www.ebilibili.com/api/playurl/{bvid}/{cid}
```

实测响应（`application/json`，约 800 字节）：

```json
{
  "bvid": "BV1GJ411x7h7",
  "filename": "【官方 MV】Never Gonna Give You Up - Rick Astley - Never Gonna Give You Up - Rick Astley.mp4",
  "part": "Never Gonna Give You Up - Rick Astley",
  "title": "【官方 MV】Never Gonna Give You Up - Rick Astley",
  "url": { "format": "MP4", "url": "https://upos-sz-estgoss.bilivideo.com/upgcxcode/.../xxx.mp4?...&deadline=1789314079&platform=html5..." }
}
```

对 `url.url` 的直链实测：
- `HEAD` **不带 Referer** → `200`，`content-length: 51973319`（≈ 52 MB），`content-type: video/mp4`
- `GET` 带 `Range: bytes=0-200000` → `206`，前 16 字节为 `\x00\x00\x00 ftypisom`（合法 MP4 头）

即：**直链可直接下载，无防盗链 Referer 限制，支持 Range 断点续传。**
注意 `deadline` 参数 → 直链有有效期，**必须拿到后立刻下载**，不可缓存复用。

#### 路径 B（兜底 1）：POST 表单

```
POST https://www.ebilibili.com/download
Content-Type: application/x-www-form-urlencoded
body: bvid=<完整链接 或 BV 号 或 b23 短链>
```

实测 `200`，返回 HTML 结果页，其中内嵌：

```html
<button onclick="startDownload('<直链>', '<filename>', '<bvid>')" class="download-btn">点击下载</button>
```

→ 可用正则 `startDownload\('([^']+)',\s*'([^']*)'` 提取直链与文件名。
页面同时包含标题 `<p><strong>标题</strong></p>`。

> 该站点表单字段名就叫 `bvid`，但实际接受**完整链接/短链**（页面 placeholder 写着"输入长/短链接"）。

#### 路径 C（兜底 2）：新平台解析页

```
POST https://www.ebilibili.com/downloads
body: url=<视频链接>
```

实测 `200`，返回"解析结果"HTML，含**标题 / 作者 / 描述 / 封面图** 与下载入口，页面声明支持 `Bilibili, 抖音, 快手, 小红书`。首版不启用，作为未来多平台扩展的预留。

#### 站点可用性注意事项（实测踩到的坑）

1. **对非浏览器请求不稳定**：直连时约一半请求出现 `ReadTimeout`（TLS 握手本身正常，TLSv1.3 协商成功）。实现必须：**超时 ≥ 15s + 重试 2 次 + 指数退避 + 复用 `aiohttp.ClientSession`**。
2. 带浏览器 UA（Chrome/124）与 `Referer: https://www.ebilibili.com/download` 时成功率明显更高，建议请求头照抄浏览器。
3. `http://` 访问会 `301 → https://ebilibili.com/download`；直接用 https 域名。
4. 这是个**个人小站**，没有 SLA：必须做「三路径依次降级 + 最终降级为发链接」，不能把它当作唯一成功路径。

### 4.4 短链解析

实测 `GET https://b23.tv/BV1GJ411x7h7` → `302`，`Location: https://www.bilibili.com/video/BV1GJ411x7h7`。
→ 短链用「禁止自动跟随 + 读 `Location`」或「跟随重定向后取最终 URL」都能拿到 BV 号。

## 5. AstrBot 侧 API 契约（官方文档确认）

| 用途 | API |
|------|-----|
| 获取当前会话模型 ID | `provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)` |
| 调用 LLM（v4.5.7+ 推荐） | `resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=...)`；文本取 `resp.completion_text` |
| 发送视频（本地文件） | `Comp.Video.fromFileSystem(path="xxx.mp4")`（要求协议端与机器人端同机） |
| 发送视频（URL） | `Comp.Video.fromURL(url="...")`（更通用，但本插件要先落地文件，故不用） |
| 发消息链 | `yield event.chain_result([...])` / `await event.send(event.plain_result(...))` |
| 事件类型过滤 | `@filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)` |
| 正则触发 | `@filter.regex(r"...")` |
| 依赖声明 | 插件根目录 `requirements.txt`，AstrBot 用 pip 自动安装 |

`metadata.yaml` 规范要点：`name` 建议小写 `astrbot_plugin_` 前缀；`display_name` 展示名；`short_desc` 市场卡片短描述；`repo`；`version`；`astrbot_version` 用 PEP 440 约束（**不带 `v` 前缀**）；`support_platforms` 取值为 `aiocqhttp` / `telegram` / `discord` / `qq_official` 等。可选 `logo.png`（256×256，1:1）、`skills/` 目录。

## 6. 目录结构

```
astrbot_plugin_bilibilidownload/
├── main.py                 # Star 入口：触发、编排、发消息、降级
├── metadata.yaml           # 插件市场元数据
├── _conf_schema.json       # WebUI 配置项
├── requirements.txt        # aiohttp（AstrBot 通常已带）/ 无需 requests
├── README.md               # 用户向说明（安装、配置、常见问题）
├── DESIGN.md               # 本文件
├── logo.png                # 可选
└── core/
    ├── __init__.py
    ├── link_parser.py      # 链接提取、BV/av 归一化、短链解析、分P识别
    ├── bili_api.py         # B 站 view / 字幕 / 热评 接口封装
    ├── ebilibili.py        # ebilibili 三路径降级获取直链
    ├── downloader.py       # 流式下载 + 体积上限 + Range 续传 + 临时文件管理
    └── summarizer.py       # LLM 总结（prompt 模板、长度裁剪、失败降级）
```

> 说明：本地兄弟插件（如 `astrbot_plugin_phoebe`）已有「`main.py` + `core` 拆分 + `StarTools.get_data_dir()` 存数据」的成熟风格，本插件沿用。

## 7. 处理流程

```
用户发消息
   │
   ├─ @filter.regex 命中链接？
   │        └─ 否 → 放行（不拦截，避免影响其他插件/LLM 对话）
   │
   ├─ 前置校验：触发范围（群/私聊）、限流、白名单群、是否已在处理同一 BV
   │
   ├─ 立刻回一条「🔍 正在解析…」（长任务先给反馈，避免群友以为机器人在装死）
   │
   ├─ 1) 链接解析     link_parser → BV 号 + 分P(cid)
   ├─ 2) 视频信息     bili_api.view() → 标题/UP/简介/时长/封面/统计
   ├─ 3) AI 总结      summarizer → LLM 生成 ≤N 字总结（可配开关）
   ├─ 4) 取直链       ebilibili：API → POST 表单 → 解析页（逐级降级）
   ├─ 5) 下载         downloader：流式写盘 + 体积上限 + 进度日志
   ├─ 6) 发送         chain = [解析卡片文本, Comp.Video.fromFileSystem(path)]
   └─ 7) 清理         删除临时文件（finally 保证）
                        │
                        └─ 任一步失败 → 降级发送：解析卡片 + 总结 + ebilibili 网页链接
```

**降级矩阵**：

| 失败点 | 降级动作 |
|--------|----------|
| B 站 view 失败 | 仍下载视频；卡片仅显示标题（从 ebilibili 返回的 `title` 取） |
| LLM 不可用/超时/未配置 | 跳过总结，正常发视频（记录 warning 日志） |
| ebilibili 路径 A 失败 | 依次试路径 B、路径 C |
| 三路径全失败 | 发送解析卡片 + `https://www.ebilibili.com/video/{bvid}` 网页链接 |
| 文件超过上限 | 不发文件，发链接 + 提示"超出体积上限（当前 N MB）" |
| 下载超时/中断 | 已下部分丢弃（或续传一次），降级发链接 |

## 8. 配置项设计（`_conf_schema.json` 草案）

```json
{
  "trigger_scope": {
    "description": "触发范围",
    "type": "string",
    "options": ["group", "private", "both"],
    "labels": ["仅群聊", "仅私聊", "群聊和私聊"],
    "default": "group",
    "hint": "在哪些会话中自动响应 B 站链接"
  },
  "need_at_in_group": {
    "description": "群聊需艾特机器人",
    "type": "bool",
    "default": false,
    "hint": "开启后群里必须 @机器人 才处理，避免刷屏；私聊不受影响"
  },
  "enable_summary": {
    "description": "启用 AI 总结",
    "type": "bool",
    "default": true,
    "hint": "使用当前会话的大模型对视频做简短总结；未配置 LLM 时自动跳过"
  },
  "summary_max_chars": {
    "description": "总结长度上限",
    "type": "int",
    "default": 200,
    "hint": "AI 总结的最大字数，过长会自动截断"
  },
  "send_video": {
    "description": "发送视频文件",
    "type": "bool",
    "default": true,
    "hint": "关闭后只发送解析信息与总结，不下载视频"
  },
  "max_video_mb": {
    "description": "视频体积上限 (MB)",
    "type": "int",
    "default": 50,
    "hint": "超过则不发文件，改为发送下载链接；建议不超过平台单文件限制"
  },
  "download_source": {
    "description": "下载源",
    "type": "string",
    "options": ["ebilibili", "ebilibili_form", "link_only"],
    "labels": ["ebilibili 直链接口", "ebilibili 表单页", "仅发送网页链接"],
    "default": "ebilibili",
    "hint": "默认走 ebilibili 直链接口，失败后自动降级到表单页"
  },
  "request_timeout": {
    "description": "请求超时 (秒)",
    "type": "int",
    "default": 20,
    "hint": "ebilibili 站偶尔响应慢，建议不低于 15 秒"
  },
  "max_concurrent": {
    "description": "同时处理任务数",
    "type": "int",
    "default": 1,
    "hint": "建议保持 1~2，避免拖垮机器人或对下载站造成压力"
  },
  "user_cooldown": {
    "description": "单用户冷却 (秒)",
    "type": "int",
    "default": 60,
    "hint": "同一用户在该时间内重复发链接将被忽略，防止刷屏"
  },
  "group_whitelist": {
    "description": "群白名单",
    "type": "list",
    "default": [],
    "hint": "留空表示所有群生效；填写群号后仅这些群生效"
  },
  "sessdata": {
    "description": "B站 SESSDATA（可选）",
    "type": "string",
    "default": "",
    "hint": "填入后可获取字幕用于 AI 总结，总结质量更好；留空则基于标题/简介/热评总结。请勿在公开场合泄露"
  },
  "send_ebilibili_link": {
    "description": "附带网页下载链接",
    "type": "bool",
    "default": true,
    "hint": "在回复中附上 ebilibili 等价链接，方便在手机上直接下载"
  },
  "verbose_log": {
    "description": "详细日志",
    "type": "bool",
    "default": false,
    "hint": "输出每一步的耗时与降级原因，排查问题时开启"
  }
}
```

## 9. AI 总结设计

### 9.1 分层策略（按可得性降级）

| 层级 | 输入 | 触发条件 | 总结质量 |
|------|------|----------|----------|
| L1 默认 | 标题 + UP主 + 时长 + 分区 + 简介 + 播放/点赞/收藏数 | 始终可用 | 一般，能说清"这是什么视频、火不火" |
| L2 增强 | L1 + 热门评论 Top10（`/x/v2/reply/main`，`sort=2`） | 评论接口可用 | 较好，能反映观众关注点 |
| L3 深入 | L2 + 字幕全文（需 `SESSDATA`） | 配置了 sessdata 且视频有字幕 | 好，接近真实内容概括 |
| L4 极致 | 音频 ASR | 不实现（成本高） | — |

**首版实现 L1 + L2，L3 留接口位。**

### 9.2 Prompt 草案

```
你是视频摘要助手。请用中文、3~5 句话总结下面这个 B 站视频，直接给结论，不要客套话，
不要编造未提供的信息。若信息不足以判断内容，就如实说明"仅凭标题与简介无法确定具体内容"。

【标题】{title}
【UP主】{owner}
【时长】{duration}
【分区】{tname}
【播放/点赞】{view} / {like}
【简介】{desc|截断 300 字}
【热门评论】
1. ...
```

调用方式（官方 v4.5.7+ 接口）：

```python
umo = event.unified_msg_origin
provider_id = await self.context.get_current_chat_provider_id(umo=umo)
resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
text = (resp.completion_text or "").strip()
```

约束：
- 超时保护（如 30s）；失败/空结果 → 跳过总结，不影响发视频。
- 输出截断到 `summary_max_chars`。
- 明确在回复里标注总结基于什么信息（`（基于简介与热评）` / `（基于字幕）`），**不假装看过视频**。

## 10. 下载与发送设计

### 10.1 下载

- 用 `aiohttp`（AstrBot 内置依赖，避免新增第三方库）流式写盘：`chunk=1MB`。
- 先 `HEAD` 或首个 `GET` 响应读 `Content-Length` 做**体积预检**，超 `max_video_mb` 直接放弃下载。
- 下载中若中断，可选做一次 Range 续传（直链支持 206）。
- 文件名清洗：去除 `/\:*?"<>|` 与 emoji 风险字符，保留中文；扩展名固定 `.mp4`。
- 落地目录：`StarTools.get_data_dir("astrbot_plugin_bilibilidownload")/tmp/`（**不要写插件自身目录**）。
- 信号量控制并发（`max_concurrent`）。

### 10.2 发送

```python
chain = [
    Comp.Plain(caption),                       # 标题 + 时长 + UP + AI 总结 + 链接
    Comp.Video.fromFileSystem(path=video_path) # 本地文件方式（协议端与机器人同机时可用）
]
yield event.chain_result(chain)
```

- 发送后再删除临时文件（`finally` 中清理，避免异常路径泄漏）。
- **备注**：`fromFileSystem` 要求协议端与机器人端在同一系统；若部署分离，需退回 `fromURL`（本项目不推荐，因为直链有时效且需 QQ 侧拉取）。
- 群聊场景建议同时附带纯文本卡片，便于用户不点视频也能看到总结。

## 11. 异常与边界清单

| 场景 | 处理 |
|------|------|
| 一个消息里有多个链接 | 只处理第一个；提示"已处理第一个链接" |
| 同一 BV 连续发送 | `user_cooldown` 内忽略；并发处理同一 BV 用锁去重 |
| 分P视频 `?p=2` | 解析 `p` 参数，取对应 `pages[p-1].cid`，卡片标注"P2" |
| 合集/番剧 `ep`/`ss` 链接 | 首版不支持，回复提示"暂不支持番剧/合集" |
| 直播间链接 `live.bilibili.com` | 不处理（放行给其他插件/LLM） |
| 付费/充电专属、大会员专享 | 下载直链大概率取不到 → 降级为发链接 + 说明 |
| 超长视频（>1 小时） | 受体积上限保护，自动降级发链接 |
| LLM 未配置 / 报错 | 跳过总结，仍发视频 |
| ebilibili 三路径全挂 | 发解析卡片 + `https://www.ebilibili.com/video/{bvid}` |
| 机器人在群内被限流 | 下载失败时日志记明确原因，不静默吞异常 |
| 临时文件堆积 | 启动时清理 `tmp/` 下超过 24h 的残留文件 |

## 12. 合规与风险提示

1. **版权**：插件面向个人自用/小群分享。README 需提示"请勿用于传播受版权保护的内容，下载后请勿二次分发"。
2. **第三方站点**：ebilibili 是无 SLA 的个人站点，接口可能随时变动/失效；设计上已做三路径降级 + 网页链接兜底，**不能承诺"永远可下载"**。
3. **账号安全**：`sessdata` 属敏感凭据，只用于读取字幕；配置项提示不要在公开渠道泄露，日志中**必须脱敏**（不打印完整值）。
4. **不绕过付费内容**：不为大会员/充电专属内容做任何规避。
5. **平台限制**：QQ 群对视频文件体积、发送频率有实际限制；大文件失败是常态，降级发链接是主要对策。
6. **对下载站友好**：单并发 + 限流 + 超时重试上限，避免给个人站造成压力。

## 13. 实施里程碑与验收标准

| 阶段 | 内容 | 验收标准（可执行） |
|------|------|--------------------|
| M1 | 脚手架：`metadata.yaml` / `_conf_schema.json` / `main.py` 骨架 / 链接识别 | 群里发链接能回"正在解析"，且能正确提取 BV 号与 `?p=N`；非 B 站消息不响应 |
| M2 | 解析：view API + 卡片渲染 | `BV1GJ411x7h7` 能输出标题/UP/时长/统计，与 `curl https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7` 一致 |
| M3 | 下载：ebilibili 三路径 + 流式下载 + 体积上限 | 对 `BV1GJ411x7h7`（实测 ≈52MB）三路径至少一路成功；断网/超时时正确降级 |
| M4 | 发送：`Comp.Video.fromFileSystem` + 临时文件清理 | 群里收到可播放视频；发送后 `tmp/` 无残留 |
| M5 | AI 总结：L1+L2 分层 + 失败降级 | LLM 正常时输出 ≤200 字总结；关闭 LLM 后插件仍能发视频不报错 |
| M6 | 打磨：限流、白名单、冷却、日志、README | 单用户 60s 内重复发链接被忽略；白名单外群不响应 |

**可复刻的实测命令（开发自测用）**：

```bash
# 1) 视频信息
curl -s "https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7" -H "Referer: https://www.bilibili.com/"

# 2) ebilibili 直链接口
curl -s "https://www.ebilibili.com/api/playurl/BV1GJ411x7h7/137649199" -H "Referer: https://www.ebilibili.com/download"

# 3) ebilibili 表单兜底
curl -s -X POST "https://www.ebilibili.com/download" \
  -H "Referer: https://www.ebilibili.com/download" \
  -d "bvid=https://www.bilibili.com/video/BV1GJ411x7h7"
```

## 14. 待确认（需作者拍板）

1. **默认行为**：群里丢链接就自动下载并发视频（体验爽，但流量/磁盘/被风控风险高），还是先只发解析+总结、需要回复关键词（如 `下载`）才发文件？**建议：默认自动，但把 `send_video` 开关放在面板显眼位置。**
2. **体积上限默认值**：50 MB 是否合适？（实测该示例视频 52 MB 已超线，可考虑 80 MB 或"仅发链接"模式更稳）
3. **触发范围**：默认仅群聊，还是群聊+私聊都开？
4. **是否需要 `sessdata` 字幕增强**：首版就做，还是先上 L1/L2、二期再补？
5. **仓库归属**：`metadata.yaml` 里的 `repo` 填 `https://github.com/wjn1121/astrbot_plugin_bilibilidownload` 是否正确？
6. **是否顺便支持抖音/快手**（走 ebilibili `/downloads` 页）：首版不做，确认无异议。

---

## 附：结论摘要

- 技术路线**已用真实请求验证可行**：B 站 view API 无 Cookie 可用；ebilibili 提供 `GET /api/playurl/{bvid}/{cid}` 直链接口，返回的 `bilivideo.com` 直链无需 Referer、支持 Range，可直接下载后作为本地文件发送。
- 最大不确定性来自 ebilibili 是个人小站（非浏览器请求偶发超时、无 SLA），因此架构上必须**三路径降级 + 网页链接兜底**。
- AI 总结在**无 Cookie 时拿不到字幕**（已实测），默认走"标题+简介+热评"路线，与市场同类插件做法一致；字幕增强作为可选二期。

---

# 附：实现决策修订（v0.1.0，编码完成后回填）

设计稿定稿后按作者拍板做了调整，实际实现以本节为准。

## 1. 交互决策（作者确认）

| 决策点 | 结论 | 落地方式 |
|--------|------|----------|
| 默认行为 | **先发解析 + 总结，不自动发视频文件** | `send_video` 默认 `false`；用户回复「下载」或 `/bdl` 按需获取 |
| 触发方式 | 保持**自动响应链接**（无需唤醒词） | `@filter.regex(TRIGGER_REGEX)`，可用 `need_at_in_group` 收紧 |
| 体积上限 | 默认 50 MB，面板数字输入框 | `max_video_mb` (`type: int`)，超限降级为发链接 |
| 字幕增强 | 不着急，首版不做 | 未引入 `sessdata` 配置项与 wbi 签名逻辑 |

## 2. 编码期新增的实测事实

1. **热门评论接口免签名可用**：`GET /x/v2/reply/main?type=1&oid={aid}&mode=3&next=0` → `code=0`，返回 20 条。
   注意 `reply/wbi/main` 反而返回 `code=-403`，因此 L2 层选用了非 wbi 路径。
2. **视频 CDN 校验 UA（关键坑）**：对下载直链做请求头矩阵实测——

   | 请求方 | UA | Referer | 结果 |
   |--------|----|---------|------|
   | aiohttp 默认 | `Python/3.x aiohttp/3.x` | 无 | **403** |
   | requests | 无 UA | 无 | **403** |
   | aiohttp / requests | Chrome UA | 无 / bilibili / ebilibili | 200 |

   因此所有出网请求必须显式带浏览器 UA，已收敛到新模块 `core/http.py`。
3. **AstrBot 插件加载机制**：`star_manager` 以 `path = "data.plugins." + root_dir_name + "." + module_str` 调用 `__import__(path, fromlist=["main"])`，
   即 main.py 的 `__package__` 是 `data.plugins.<插件目录名>`，因此**可以安全使用 `core/` 子包 + 相对导入**（不需要 `sys.path` hack，也不必挤成单文件）。
4. **体积预检生效于下载之前**：HEAD 拿到的 `Content-Length` 超限时直接抛 `VideoTooLarge`，不会先下几百 MB 再放弃。

## 3. 实际目录结构

```
astrbot_plugin_bilibilidownload/
├── main.py               # Star 入口：触发、编排、下载、发送、降级、限流
├── metadata.yaml         # 插件市场元数据（astrbot_version: ">=4.16"）
├── _conf_schema.json     # 14 个面板配置项
├── requirements.txt      # aiohttp
├── README.md             # 用户向说明
├── DESIGN.md             # 本文件
├── .gitignore
└── core/
    ├── __init__.py
    ├── http.py           # 共享请求头（浏览器 UA）—— 编码期新增
    ├── link_parser.py    # 链接识别/归一化（纯函数，可单测）
    ├── bili_api.py       # view / 热评 / 短链跳转
    ├── ebilibili.py      # 三路径降级取直链
    ├── downloader.py     # 流式下载 + 体积上限 + 临时文件清理
    ├── formatting.py     # 时长/数字/文件名/卡片渲染
    └── summarizer.py     # LLM 总结（含"依据"标注）
```

## 4. 验收结果（2026-09-13 实跑）

自检脚本（用 stub 顶替 astrbot SDK：离线 26 项 + 在线 10 项）**36 项全部通过**，其中端到端实测：

- `get_view` 返回标题/UP/时长/播放量，耗时 ~0.6s
- `get_hot_replies` 取到 10 条热评
- ebilibili 直链接口命中（`source=api`）
- 体积超过上限时正确抛 `VideoTooLarge` 并降级为发链接，且不落盘、无残留
- **真实下载成功**：49.6 MB / 5.3s，文件头为合法 MP4（`ftyp`）
- 未配置 LLM 时跳过总结，仍正常发出视频与卡片

已知未覆盖项：真实 QQ 平台的消息发送（需实际 AstrBot 环境）、分P视频下载、B 站风控下的长期稳定性。

## 5. v0.1.1 修复：发送阶段的 `retcode 1200 路径不存在`

### 现象（作者实机日志）

```
[bilibili-download] 下载完成 倒霉糯：吃饭的路如此艰难吗？.mp4（14.7MB，耗时 13.2s）
[respond.stage] Prepare to send - [ComponentType.Node]
[respond.stage:310] 发送消息链失败: ... error=<ActionFailed retcode=1200 message='路径不存在'>
```

下载是成功的，**失败发生在发送环节**。

### 根因

原实现用 `yield event.chain_result(chain)` 交出消息链，而清理代码写在生成器的 `finally` 里：

```python
yield event.chain_result([Plain(caption), Video.fromFileSystem(path)])   # 交给 AstrBot
finally:
    unlink(path)     # ← 生成器在 AstrBot 真正发送之前就恢复执行，文件被删掉了
```

AstrBot 的 pipeline 是"先收集 handler 的 yield 结果、再在 respond stage 统一发送"，
生成器一 yield 就继续往下跑，`finally` 的删除发生在 respond stage 发送之前，
于是协议端去读文件时文件已不存在 → 「路径不存在」。

日志时间戳也印证了这一点：`下载完成`(18.750) → `Prepare to send`(18.754) → `失败`(18.761)，中间只隔 7ms。

### 修复

1. **发送改为显式 `await event.send(...)`**，等平台适配器真正发完再删临时文件
   （`event.send` 会置位 `_has_send_oper`，配合 `event.stop_event()` 阻止默认 LLM 重复应答）。
2. **新增 `video_send_mode` 配置**（`auto` / `file` / `url`）：
   - `file`：`Video.fromFileSystem(path)`，要求协议端能读到 AstrBot 的文件；
   - `url`：`Video.fromURL(直链)`，把 ebilibili 直链交给协议端拉取，**不依赖共享文件系统**、也不占机器人磁盘与中转流量；
   - `auto`（默认）：先 file，发送失败自动降级 url。
3. **文本卡片与视频分开两条消息发送**：长文本 + 视频的混合链会被 AstrBot 包装成合并转发（Node），
   拆开后视频消息链只有 `Video` 一个组件，规避 Node 里视频段的各种平台差异。
4. **失败可感知**：发送异常会被捕获并记录，全部方式失败时回复用户可操作的提示，而不是静默失败。

### 相关环境事实（作者实机日志）

同一实例上 `astrbot_plugin_parser` 也报了同样的 `retcode 1200`（它发送的是自己 cache 目录里的 mp4，
文件名纯 ASCII）。这说明**该部署下协议端与 AstrBot 很可能不在同一文件系统**，
因此 `url` 模式在这个环境里是更可靠的选项；`auto` 模式则两种情况都能走通。

### 验证

自检脚本新增三项针对性断言（用 stub 顶替 astrbot SDK）：

- `_send_video` 在"本地文件方式抛异常"时自动改用直链并成功；
- 发送 `Video(fromFileSystem)` 的那一刻文件**仍然存在**（防止时序回归）；
- 全部发送结束后临时目录无残留。


