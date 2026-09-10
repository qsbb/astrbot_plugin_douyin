# 接口与协作边界

中文项目名为“拾趣”，插件展示名为“凝心溯溪-趣”，Page 标题为“拾趣 · 抖音工作台”。技术 ID `astrbot_plugin_douyin`、Python 工程名 `astrbot-plugin-douyin`、命令前缀 `/dy` 和当前开发版本 `0.0.1` 保持不变。

所有工具都由 AstrBot 原生 `@filter.llm_tool` 注册。调用者来自 `event.unified_msg_origin`、`event.get_sender_id()`、`event.is_admin()`，模型不能填写或更换宿主身份。站内写入要求管理员角色、调用会话名单、调用者名单、具体动作持续授权、绑定账号及必要的目标名单同时成立。

## 工具结果

外层使用 `douyin.result.v1`：

```json
{
  "schema_version": "douyin.result.v1",
  "request_id": "intent-example-0001",
  "status": "submitted",
  "code": "COMMENT_SUBMITTED",
  "source_trust": "untrusted_platform_content",
  "data": {}
}
```

`source_trust` 是工具边界说明，不能替代上层模型对网页/评论提示注入的防护。其他插件可以解析工具结果，但本插件没有调用其私有 API、写入其他插件数据库或模拟用户历史消息。

内容感知数据使用 `douyin.observation.v1`，包含作品引用、公开链接、页面描述、真实音轨转写、视觉提取、采样覆盖、缺失项及事实核验状态。当前 STT 接口返回字符串，因此没有伪造逐句时间戳。

站内写操作返回 `douyin.receipt.v1`，包含账号、操作、请求 ID、目标对象、提交状态与核对证据。正文仅保留摘要哈希作为技术审计依据，不作为人格长期记忆。去重键绑定宿主会话、调用者、抖音账号、请求 ID，参数指纹防止同一请求 ID 被复用于其他动作。

`douyin_receipt` 是只读查询工具；`douyin_task` 只管理内存中的内容感知任务，不作为消息调度器。任务结果默认仅在发起任务的宿主会话和调用者下可见，重启后任务号失效，不自动恢复模型调用。

## Page 接口

原生 `pages/manager` 通过 `window.AstrBotPluginPage` 的 `apiGet` / `apiPost` 调用后端。后端使用公开 `astrbot.api.web.request` 与 `context.register_web_api()`，路由前缀为 `/astrbot_plugin_douyin/page/`，不开放独立端口。

| 方法与路径 | 输入 | 用途 |
|---|---|---|
| GET status | 无 | Bot 浏览器账号、接管状态、可编辑基础配置 |
| POST control | `{action}` | `acquire` / `release` / `login` / `home` / `inbox` / `reload` |
| GET frame | 无 | 内存 JPEG data URL、尺寸、一次性 frame_id；最多 2 MiB JPEG |
| POST input | `{kind,frame_id,...}` | click(x,y)、text(text)、key(key)、scroll(delta_y,delta_x?)、drag(points) |
| POST action | `{operation,params,request_id?}` | 与 Bot 相同语义执行器，写操作与 receipt 查询必须提供 request_id |
| POST bind | `{}` | 从浏览器实测当前账号并保存绑定 |
| POST settings | `{config}` | 严格允许 enabled、allowed_actions/target_refs/origins/actor_ids |
| POST pause | `{paused}` | 复用现有暂停/恢复入口 |
| GET receipts | 无 | 最近 20 条结构化操作回执，不重复提交 |

Page 结果使用 `{result: <douyin.result.v1>}`，避免 SDK 将 `status=ok` 的 data 自动解包后丢失 request_id。响应携带 `Cache-Control: no-store`。认证失败和超大/畸形请求使用标准宿主错误响应。

身份仅取 `request.username`，转换为独立的 `DashboardCaller`；空用户名和 `api_key:` 身份被拒绝，前端不能提供 UMO、sender 或 is_admin。Dashboard 操作者无需冒充聊天账号或加入聊天会话名单，但结构化写动作仍需启用、绑定账号、动作授权与目标名单。LLM 工具不能创建 DashboardCaller，也不能调用 Page 专用入口。

人工接管使用 300 秒空闲租约；同账号浏览器由 Page 与 Bot 共用一把服务锁。frame/input 请求续约，控制画面 ID 在 30 秒后或一次输入后失效。输入日志不包含正文、验证码或截图。人工输入只返回 `submitted / MANUAL_INPUT_DISPATCHED`，没有语义动作已确认的含义。

设置保存和运行时配置替换在同一串行事务内，磁盘保存委托公开 `save_config_async()`。不支持该接口时页面配置只读。插件关闭后 PageApi 标记关闭，残留路由只返回 503；不访问宿主私有路由列表。

## 系列契约

- `series.diagnostics@1.0`：插件实现 `diagnostic_log_contract()`、`diagnostic_events(after_seq,limit)`、`diagnostic_clear()`。1000 条内存环形缓冲，UTC 时间、正确分页、流 ID、凭据脱敏。
- `plugin.health@1.0`：通过类常量 `PLUGIN_HEALTH_CONTRACT` 声明；健康状态指初始化/停止状态，不等于抖音站点可用或登录成功。
- `series.webui@1.0`：只读状态面板，未知面板/动作明确拒绝。没有第二套 HTTP 控制台，也不接受面板 payload 自报角色后执行社交动作。
- `series.model_router@1.0`：消费经过身份与版本校验的路由。提供方缺失时回退；提供方存在但契约无效时记录警告、停用联动后走显式配置/宿主路径。

本插件不声明 `series.control@1.0`，因为当前没有核托管的配置覆盖层；不写入 `ningxin.request_context.v1`，也不新增请求上下文 owner。因此本次无需修改六份共享副本或占用 LLM 钩子优先级。

## 核接入前的具体工作

源码仓库已创建为 [qsbb/astrbot_plugin_douyin](https://github.com/qsbb/astrbot_plugin_douyin)，没有改动现有核仓库。正式纳管前需要：

1. 用户已确定展示名单字“趣”：中文项目名“拾趣”、插件展示名“凝心溯溪-趣”，命令前缀仍为 `/dy`。当前本地查重未发现冲突；核的正式登记及共享命名表同步仍待维护方处理。
2. 真实仓库 URL 已写入 `metadata.yaml`，正式纳管时核对它与可信登记一致。
3. 由核维护方在 `core/trusted.py` 中登记 `plugin_id=astrbot_plugin_douyin`、展示名和真实仓库 URL，并同步现行规范的命名与命令表。
4. 核的模型路由声明补齐 `plugin_id=astrbot_plugin_update_manager`、`series_id=ningxin_suxi`；调用方已经按规范准备好校验。现有声明缺少两项身份字段，不能声称这条联动已验收。
5. 验证核的诊断聚合、只读面板发现和更新健康检查，再按用户授权进入发布或部署流程。

## 源码与官方文档依据

- [AstrBot 原生工具与模型调用](https://docs.astrbot.app/dev/star/guides/ai.html)：采用公开装饰器与 Context 调用，未导入 `astrbot.core`。
- [AstrBot v4.28.0 Context](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.0/astrbot/core/star/context.py)：核对宿主会话模型、STT、插件元数据和工具生命周期入口。
- [AstrBot 工具注册源码](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.0/astrbot/core/star/register/star_handler.py)：工具参数来自 Google 风格 docstring，本项目逐项检查声明参数。
- [Playwright 持久化上下文](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[网络响应事件](https://playwright.dev/python/docs/network)：用于隔离登录与监听页面真实响应。
- [douyin-cli 页面实现参考](https://github.com/Yht20927/douyin-cli/blob/main/scripts/douyin.user.js)：仅核对当前用户、点赞和评论接口的请求/回执形状，没有复制其签名、代理桥接、人格或私信协议代码。

所有页面选择器仍需以真实登录后的页面证据为准。尤其原生转发和私信不能仅凭上述接口参考推出线上兼容性。
