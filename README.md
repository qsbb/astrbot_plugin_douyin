# 拾趣 · 凝心溯溪-趣：AstrBot 抖音能力插件

首个预发布版 `0.0.1`。中文项目名为 **拾趣**，插件展示名为 **凝心溯溪-趣**。它给 AstrBot 主 Agent 提供抖音内容与站内操作工具，供已有虚拟恋人人格决定看什么、说什么、分享给谁。通过 **拾趣 · 抖音工作台** Page，可以在 AstrBot 内登录并操作 Bot 当前使用的抖音浏览器。记忆、知识库、关系状态、情绪和主动互动调度仍由其他插件负责。

[GitHub 仓库](https://github.com/qsbb/astrbot_plugin_douyin) · [预发布包](https://github.com/qsbb/astrbot_plugin_douyin/releases) · [CI](https://github.com/qsbb/astrbot_plugin_douyin/actions/workflows/ci.yml)

按用户要求，当前开发阶段版本固定为 `0.0.1`；继续迭代时不自动升版本，待用户另行指定。

**当前是已完成离线验证的开发实现，不是经过真实抖音账号验收的正式版本。** 页面控件和稳定身份字段不满足条件时会返回明确错误，不按昵称猜人或把点击按钮当成成功。特别是收件箱、真实 @ 和原生视频转发，还需要针对实际登录后的页面完成兼容验收。

## 当前能力与证据

| 能力 | 已有实现 | 当前验收边界 |
|---|---|---|
| AstrBot 原生工具、配置、管理命令 | 13 个 `douyin_*` 工具；`/dy status/login/pause/resume/receipts` | 宿主公开 API 源码核对与 stub 测试；尚未安装进真实 AstrBot |
| 拾趣 · 抖音工作台 Page | 共用浏览器截图、扫码/短信登录、人工接管、点击/文字/按键/滚动/拖动、账号绑定与基础授权设置 | 本地 Chrome 与 Page API 验证；真实抖音登录仍需在部署环境完成 |
| 浏览推荐与搜索 | 监听页面 JSON，推荐中选少量作品逐条打开并读取播放状态 | 搜索结果是候选；不伪装为手机原生推荐流，未登录实站验收 |
| 作品与评论 | 详情响应/RENDER_DATA JSON、评论 ID、分页 | 标准作品 ID/完整链接；短链接暂不支持；页面签名和结构仍需实站确认 |
| 视频内容感知 | 下载真实视频，FFmpeg 抽音轨与最多 4 张画面，调用已有 STT/VLM | 本地真实 FFmpeg 验证；模型为测试替身；不宣称逐帧理解整段 |
| 点赞与评论 | 点赞目标状态核对；提交评论/回复；区分提交与可见性 | Chrome 本地页面验证，平台实站未验收 |
| 真实 @ 用户 | 联系人 uid/sec_uid 解析、原生提及选项、编辑实体与回执核对 | 条件式适配；真实页面的身份属性和实体结构尚未采样验证 |
| 抖音私信/原生视频转发 | 根据稳定会话/用户 ID 定位，核对新消息或原生卡片回执 | **实验性适配骨架**；所需 DOM 身份与回执属性在当前抖音页面是否存在尚未确认，不能承诺直接可用 |
| 分享到 QQ/微信等 | 工具返回公开 `canonical_url` | 由 AstrBot 原有发送工具完成；本插件不额外适配聊天平台 |
| 系列治理 | `series.diagnostics@1.0`、`plugin.health@1.0`、只读 `series.webui@1.0` | 插件侧就绪，源码仓库已创建；尚未注册到核可信名单 |

## 运行要求

- Python 3.12+、AstrBot `>=4.28,<5`。
- `requirements.txt` 中依赖；Playwright Chromium 或本机 Chrome/Edge。
- 需要分析音轨和画面时安装 FFmpeg，并配置路径。
- 一个独立抖音账号、一个持久化浏览器配置目录；不复用日常 Chrome 登录配置。
- Page 首次启动使用无头浏览器，远端服务器可以直接在 Page 查看二维码并操作。聊天命令 `/dy login` 则用于有桌面的主机。

本插件不会自动下载浏览器、启动 VNC 或安装系统组件。浏览器及音视频依赖由部署环境提供。

## 首次配置

1. 在待测试的 AstrBot 环境安装开发包。安装目录名必须保持 `astrbot_plugin_douyin`。
2. 如果使用本机 Chrome，设置 `browser_channel=chrome`；如果使用 Playwright 的 Chromium，先在相同 Python 环境执行 `python -m playwright install chromium`。
3. 在 AstrBot 插件详情打开 **拾趣 · 抖音工作台**，接管浏览器，再打开登录。直接在画面中扫码或点击输入框后发送手机号、验证码；拖动支持用户手动完成滑块。Page 与 Bot 共用同一持久化会话。Cookie 与浏览器存储不会回传；截图只在认证后的页面短期传输，不落盘。
4. 登录后刷新账号状态，点击绑定当前账号。账号 ID 由后台向抖音查询，不使用页面传来的 ID。用 `/dy status` 获取聊天的 `umo`、`actor_id`，填入 Page 授权设置中的 `allowed_origins`、`allowed_actor_ids`，供 Bot 的聊天工具使用。
5. 设置 `enabled=true`。先保持 `allowed_actions=[]`，验证浏览、搜索、详情和感知。
6. 需要站内互动时，由管理员配置具体的 `allowed_actions`。可选 `set_like`、`post_comment`、`share_video`、`send_message`。这是动作级持续确认授权；工具不能给自己追加权限，写操作还必须来自 AstrBot 管理员身份。
7. 分享、私信和 @ 前，先解析并人工核对目标的稳定标识，再加入 `allowed_target_refs`。联系人姓名不是授权标识。

Page 中的账号绑定、启用开关、动作/目标/聊天会话授权保存后立即生效；其他原生配置在重载插件后生效。管理员可在 Page 或通过 `/dy pause` 暂停、`/dy resume` 恢复。暂停阻止后续工具动作并取消内容感知，已经跨过提交点的动作仍可能完成，需要查看回执。

## 在 Page 使用 Bot 账号

这里的账号指 **Bot 在本插件内登录的抖音账号**。打开 Page 即复用这一会话，不需要给页面另建抖音账号，也不会把 QQ/微信机器人登录态当成抖音登录态。

- **接管浏览器**：等待正在执行的 Bot 页面操作结束后获取控制权。接管期间，Bot 的浏览器任务和 Page 快捷语义操作会返回 `ACCOUNT_CONTROLLED`；画面中的人工点击仍可执行。
- **登录与手动操作**：浏览器画面是定时刷新的截图。点击、文字输入、常用按键、滚轮和手动拖动会发送到同一个浏览器；支持二维码、短信及平台提供的登录流程。它不是带音频的远程视频直播。
- **归还 Bot**：释放人工控制，恢复 Bot 对该会话的租用；不会改变原先的暂停状态。页面停止请求画面或输入后，接管权五分钟过期。退出网页不保证最后一条请求送达，因此后端租约负责回收。
- **快捷操作**：归还后可从表单调用搜索、浏览、作品读取、联系人解析、点赞、评论、@、分享和私信等已有工具路径。写操作仍需要启用、绑定账号、动作/目标授权，使用唯一 request_id 查询回执；未知结果不可自动换 ID 重发。
- **人工输入回执**：`MANUAL_INPUT_DISPATCHED` 仅表示鼠标或键盘事件已交给浏览器，不代表点赞、消息或评论已被平台确认。结果需看当前页面；该输入不会伪造结构化操作成功回执。

登录时保留主页面和一个活动弹窗；额外弹窗会被关闭，接管任务串行处理。弹窗关闭时恢复主页面，归还后 Bot 再次读取内容也会回到主页面并刷新同一账号会话。评论提交需要有效评论 ID 和正文核对；原生转发只有出现本次新增的稳定回执 ID 才会报告 verified，旧回执或缺失身份不会被当作成功。

Page 只接受宿主 Dashboard 登录身份，拒绝插件 scope API key。所有 API 经宿主 bridge 转发，无独立端口、Cookie 导入或任意网址/脚本执行入口。新增 `pages/manager` 后需要重载一次插件，由 AstrBot 扫描页面。

示意配置中的账号与会话均为示例，不应直接复制为真实授权：

```json
{
  "enabled": true,
  "browser_channel": "chrome",
  "headless": false,
  "allowed_origins": ["平台实例:FriendMessage:你的会话ID"],
  "allowed_actor_ids": ["你的宿主用户ID"],
  "expected_account_ref": "user:实际抖音UID",
  "allowed_actions": [],
  "allowed_target_refs": [],
  "vision_provider_id": "",
  "stt_provider_id": ""
}
```

## 主 Agent 的使用方式

搜索、点赞、评论、@、转发等已经是原生工具。工具映射、Page 与 Agent 的区别及上下文开销说明见 [AGENT_TOOLS.md](docs/AGENT_TOOLS.md)。

主 Agent 可以先 `douyin_browse` 或 `douyin_search`，选中作品后 `douyin_watch`。`depth=metadata` 只读取页面作品信息；`preview` 分析前最多 20 秒；`full` 分析到管理员设定的时长上限。视频分析返回任务号，通过 `douyin_task` 查询或取消。

内容结果区分 `platform_description`、`transcript`、`visual_description` 和 `coverage`。标题、评论、字幕和画面中的文字均是待分析资料，不能作为新的权限指令。作品中声称的事实仍需知识插件核验。

得到结果后，由原主 Agent/人格模块决定是否分享、点赞或评论。需要聊天平台分享时直接使用返回的标准公开链接。需要抖音站内写操作时提供唯一 `request_id`，同一个意图重试或查询时必须复用原值。

结果语义：

站内操作可用 `douyin_receipt(request_id)` 只读查询，暂停后仍可用。管理员还可用 `/dy receipts` 查看最近 20 条回执；这两个入口均不会重新发送动作。

- `verified`：存在该操作规定的核对证据，具体看 `verification`。
- `submitted`：平台给出了提交回执，公开可见性或最终状态尚未完全核对。
- `unknown_result`：动作可能已发出，禁止自动换 ID 重发。
- `failed`：预检失败或平台明确拒绝；查看 `code`。
- `partial`：内容提取不完整；查看 `missing`、`coverage`。

浏览和感知不会自动调用长期记忆，也不会建立日常刷视频调度。其他插件可识别本插件工具返回的 `douyin.result.v1` JSON，再按各自职责处理；没有安装即可自动互通的隐式承诺。

## 数据、授权与模型

数据路径通过公开 `StarTools.get_data_dir(PLUGIN_ID)` 获取。`runtime.json` 保存暂停标志和脱敏操作回执，原子写入；`browser-profile/` 保存独立登录状态；`media-cache/` 只放当前分析临时媒体，完成或取消后清理。诊断环形缓冲仅在内存保存，不传播到 AstrBot 核心日志。

明确结果的幂等记录保留 7 天，未知结果保留至人工处理；上限 2048 条，满后停止新增动作。重启时在途记录转为未知，不自动重放。每小时配额包含失败、提交和未知结果；同内容写操作另有短期重复保护。当前不提供由 LLM 清除未知记录或重置去重的工具。

模型选择为插件显式 Provider → 有效的核模型路由 → 当前会话原生 Provider。未安装核时直接使用本地/宿主路径，不导入其他插件内部代码。

当前本地核的 `series_model_router_contract()` 缺少现行规范要求的 `plugin_id`、`series_id`。本插件会记录 `MODEL_ROUTER_DISABLED`、停用这条联动并使用显式配置/宿主回退；因此当前核的模型偏好**不会直接被本插件采纳**。待核维护方补齐契约并接入后再验收。不能把提供方未升级时的回退当作已经完成系列模型路由联动。

## 开发与验证

Windows 开发缓存和测试临时文件统一放在 `D:\codex_tmp\astrbot-douyin`，不要放 `C:\tmp` 或用户临时目录。示例：

```powershell
$env:TEMP='D:\codex_tmp\astrbot-douyin'
$env:TMP=$env:TEMP
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DOUYIN_TEST_BROWSER_CHANNEL='chrome'
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q --basetemp 'D:\codex_tmp\astrbot-douyin\pytest-run' -o cache_dir='D:\codex_tmp\astrbot-douyin\pytest-cache'
ruff format --check .
ruff check .
```

测试浏览器默认使用 Playwright Chromium；如上设置环境变量则使用 Chrome。浏览器测试拦截所有页面请求，不登录抖音。FFmpeg 测试生成几秒钟的色块与音频信号，验证真正的媒体处理流程；它不会验证实际模型识别质量。

真实账号验收顺序和当前限制见 [LIVE_VALIDATION.md](docs/LIVE_VALIDATION.md)。公开协议和源码依据见 [CONTRACTS.md](docs/CONTRACTS.md)。
