# bupt_news 插件

> 上级：[插件文档](README.md)

每 10 分钟同步一次北邮信息服务门户的「校内通知」，把**新增**通知推送到指定的 QQ 群 / QQ 用户。
- **免打扰**：默认 23:00–07:00 不推送，次日首次越过窗口时随**当天天气**汇总补发
- **短 ID**：每条通知分配 `#1` 递增编号，可用 `/buptnews get <编号>` 查回全文（**图文按原排版**）与原文链接（含附件入口）
- **20 字摘要**：推送只含 DeepSeek 生成的 ≤20 汉字梗概（批量调用；失败降级为标题截断）
设计规格见 `specs/2026-09-24-bupt-news-digest-design.md`，实施计划见 `plans/2026-09-24-bupt-news-digest.md`。
相关文档：[`usage.md`](https://github.com/Green1423/ncqqpy/blob/main/docs/guide/README.md)（框架使用）· [`plugin-dev.md`](https://github.com/Green1423/ncqqpy/blob/main/docs/plugin-dev/README.md)（插件开发）· [`ai-chat.md`](https://github.com/Green1423/ncqqpy/blob/main/docs/plugins/ai-chat/README.md)（同款「服务范围」闸门）· [`byrdocs.md`](https://github.com/Green1423/ncqqpy/blob/main/docs/plugins/byrdocs/README.md)（白名单）· [`ncqq-watchdog.md`](https://github.com/Green1423/ncqqpy/blob/main/docs/reference/watchdog/README.md)（服务器看门狗）。

## 章节

| 章节 | 小节 |
|------|------|
| [目录结构](01-layout.md) | 1.1 插件目录 · 1.2 配套脚本与入口 |
| [运行前提](02-requirements.md) | — |
| [配置](03-config.md) | 3.1 配置项 · 3.2 凭据优先级 |
| [命令](04-commands.md) | 4.1 命令一览 · 4.2 私聊可用范围（`binding`）—— 两道闸门，别混为一谈 · 4.3 权限实际效果（已实测） · 4.4 三个容易踩的点 … |
| [作用域（命令能在哪里用 / 影响什么）](05-scope.md) | 5.1 命令的 `scope` 参数 —— 子命令各不相同 · 5.2 真正决定「机器人听哪个群」的是 `bound_groups` · 5.3 命令影响的「数据作用域」是全局的 |
| [工作流程](06-workflow.md) | 6.1 同步与推送流程 · 6.2 正文表格渲染为图片 · 6.3 @全体成员 · 6.4 学生通知判定（不 @全体成员） … |
| [已知限制](07-limitations.md) | — |
| [排障](08-troubleshooting.md) | 8.1 WebSocket 连不上：先看异常类型 · 8.2 正确配置 · 8.3 三步自查 · 8.4 摘要服务（DeepSeek）的三个坑 … |
| [与 Node 爬虫的关系](09-node-crawler.md) | — |
| [测试与自检](10-tests.md) | — |
