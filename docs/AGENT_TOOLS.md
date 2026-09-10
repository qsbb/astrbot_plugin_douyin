# Agent 工具与上下文开销

本文说明“拾趣”项目的“凝心溯溪-趣”插件工具。人工操作入口为 **拾趣 · 抖音工作台** Page。

## 已经封装的工具

这些接口已通过 AstrBot 原生 `@filter.llm_tool` 注册。Page 快捷操作与它们调用同一个 `DouyinService`，没有第二套社交执行逻辑。

| 需求 | 当前工具 | Agent 需要提供的主要信息 |
|---|---|---|
| 搜索视频 | `douyin_search` | query、limit，默认 5 条 |
| 刷推荐 | `douyin_browse` | limit、dwell_seconds，默认 1 条 |
| 读取／理解作品 | `douyin_watch` | video_ref、depth、question |
| 读评论 | `douyin_read_comments` | video_ref、limit、cursor，默认 20 条 |
| 找好友／用户 | `douyin_resolve_contact` | query、limit，返回稳定 target_ref |
| 点赞／取消点赞 | `douyin_set_like` | video_ref、liked、request_id |
| 评论／回复 | `douyin_post_comment` | video_ref、text、request_id，可选 reply_to |
| 评论中 @ 用户 | `douyin_post_comment` 的 mentions_json | 已核对的 target_ref 数组，不用昵称冒充身份 |
| 抖音内原生视频转发 | `douyin_share_video` | video_ref、target_ref、request_id |
| 读取会话／消息 | `douyin_read_inbox` | conversation_ref、limit |
| 发送私信 | `douyin_send_message` | conversation_ref、text、request_id |
| 核对任务和结果 | `douyin_status`、`douyin_task`、`douyin_receipt` | 按需提供任务号或原 request_id |

这是 **13 个工具**；@ 是评论的一个参数，不额外注册重复工具。站内转发与把公开链接分享到 QQ/微信不同：后者由 AstrBot 原有发送能力处理。

授权、账号核对、目标解析、页面控件定位、提交和回执检查由程序完成。Agent 负责意图与内容决策，不必逐步决定鼠标坐标。真实 @、私信与原生转发仍有实站 DOM/回执适配限制；封装为工具不等于这些网站适配已经上线验收。

## 哪些环节消耗模型资源

| 路径 | 当前实现 | 开销来源 |
|---|---|---|
| 人工使用 Page 的截图和鼠标键盘 | 浏览器截图传给认证页面，页面输入交给 Playwright | 不调用模型；仍占用浏览器资源和网络带宽 |
| Agent 调用搜索／点赞／评论等语义工具 | Agent 传结构化参数，程序操作浏览器，再返回 JSON | 工具定义、调用参数、工具结果和后续模型决策 |
| `watch(depth=metadata)` | 读取作品描述和元数据 | 工具上下文；不额外调用 STT/VLM |
| `watch(depth=preview/full)` | 下载媒体、FFmpeg 抽音轨及最多 4 张采样帧，调用已配置的 STT/VLM | 模型调用、图像／音频处理以及返回给主 Agent 的内容；具体计费取决于 Provider |
| 让视觉 Agent 连续看屏幕并点击 | 当前没有给 Agent 注册远程截图／坐标输入工具 | 若以后增加此模式，会产生多轮截图输入、观察与动作决策 |

目前 `douyin_watch` 工具默认 `preview`，不是 metadata。preview 分析前最多 20 秒；full 分析到配置上限。Page 的鼠标操作不会调用感知模型，但在 Page 快捷表单中主动选择视频感知仍会调用相应 Provider。

## 优先改进方向（尚未实现，不作为当前功能承诺）

1. **为 Agent 单独提供精简结果视图。** 候选只返回稳定 ID、短标题、作者和必要状态，详细信息按需展开；保持 Page 和内部回执完整。不能在 JSON 序列化后按字符硬切，以免破坏结构、request_id 或结果状态。
2. **把内容读取与内容理解按需求分开。** 搜索、候选筛选先取 metadata，确实需要知道视频内容时再执行 preview。不能仅为了省开销就把标题当成已经看过的视频。
3. **限制列表与长文本。** 当前搜索默认 5 条、评论默认 20 条；作品标题和单条评论的解析上限均为 4000 字符。当前总结果没有统一 token 预算，长结果仍可能占据很多上下文。
4. **减少重复分析和无效轮询。** 使用原任务号、原 request_id 查询；可进一步为同视频、同分析参数增加短期技术缓存。它属于运行缓存，不接管长期记忆、人格或兴趣系统。
5. **由宿主按任务选择工具集合。** 当前插件会注册全部 13 个工具，是否在每轮都把定义送入模型取决于宿主。按需注入可减少无关工具描述，但应与现有互动／编排插件合作，不在这里另建 Agent。

实际开销要分别记录主 Agent 输入／输出、STT 音频时长、VLM 图像／文本用量和调用轮数。当前尚未采集真实 Provider 用量，不能给出固定 token 数或节省百分比。
