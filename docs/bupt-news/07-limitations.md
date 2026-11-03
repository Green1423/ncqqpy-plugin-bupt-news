# 七、已知限制

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 上一章：[工作流程](06-workflow.md) · 下一章：[排障](08-troubleshooting.md)


- **⚠️ DeepSeek 摘要是 best-effort**：偶发失败时该批降级为**标题截断**（不会丢通知），
  失败会以 `[bupt_news] 摘要失败（N 条将降级）` 记入日志；实测在连续多轮调用中出现过整批
  返回空的情况。重试用尽时本轮**不写降级摘要**，留给下次同步或调取时再试（见
  (08-troubleshooting.md#85-ai-摘要重试与时间预算)）。
- **⚠️ 导入历史前请先停掉 bot**：`import_history.py` 会写 `state.json` 与
  `notices.jsonl`；若 bot 同时在跑，它内存里的旧状态会在下次 `save()` 时覆盖
  `state.json`，造成两者不一致。`StateStore` 已加**自愈**——加载时会把
  `idCounter` 抬到既有记录的最大 ID（只增不减），避免之后 `add_notice()` 分配出
  与既有记录冲突的 ID；但仍建议停 bot 再导入。
- **依赖 NapCatQQ**：本插件只负责「抓取 + 调用发送 API」，真正的 QQ 登录与发送由
  NapCatQQ（OneBot11）完成。
- **门户为校内资源**：`my.bupt.edu.cn` 需校园网或 atrust/VPN；Bot 所在主机必须能访问它。
- **本机 `systemd-resolved` 偶发解析失败**（`Name or service not known`），插件已内置重试，
  表现为「本轮跳过，下轮恢复」。

---
