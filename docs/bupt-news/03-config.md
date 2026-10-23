# 三、配置

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 上一章：[运行前提](02-requirements.md) · 下一章：[命令](04-commands.md)


## 3.1 配置项

配置位于 `config.json` 的 `plugins.bupt_news`：

| 键 | 默认 | 说明 |
|----|------|------|
| `interval` | `600` | 同步间隔（秒），最小 60 |
| `enabled` | `true` | 推送总开关；`false` 时仍同步落库但不发送，**重新开启后会补发**（与免打扰同一套积压机制）|
| `columns` | `[1154]` | 栏目 `wbtreeid`，1154=校内通知，1305=公示公告，1221=校内新闻 … |
| `targets` | `{groups:{}, users:{}}` | **绑定表**（唯一来源）：一项 = 一个群/用户 + `message`（推通知）/ `event`（推活动）/ `token_file`。在表里即**准入**。用 `/buptnews add\|set\|del` 维护；旧的 `groups`/`users`/`binding`/`afterclass.bindings` 仍能自动合成，但会被 `targets` 取代 |
| `quiet_hours` | `{start:"23:00", end:"07:00"}` | 免打扰窗口，支持跨午夜；同日窗口（如 01:00–06:00）也支持 |
| `weather` | `{enabled,label,latitude,longitude,timeout,umbrella_pop}` | Open-Meteo 天气（免 key）；`umbrella_pop` 默认 50 = 降水概率达 50% 就提醒带伞 |
| `summarize` | `{enabled,base_url,model,api_key,batch_size,max_tokens,timeout,reasoning_effort,retry,retry_delay,retry_on_notice,retry_scan_max,retry_cooldown,retry_timeout,retry_batch_size,push_budget,retry_budget}` | DeepSeek 摘要；`batch_size` 默认 3，`max_tokens` 默认 4096；失败重试见 (08-troubleshooting.md#85-ai-摘要重试与时间预算) |
| `data_dir` | 插件目录下 `data/` | 状态与记录存放目录。**测试/多实例必须覆盖**，否则会写到真实数据上 |
| `digest_max_items` | `40` | 早报最多列出多少条（超出只显示最新 N 条 + 提示）|
| `list_page_size` | `20` | `/buptnews list` 每页条数 |
| `notice_retention` | `500` | 正文保留条数（供 `/buptnews get` 查询）|
| `max_push_per_round` | `8` | 单轮最多推送几条，防止刷屏（**早报不受此限制**）|
| `first_run_backfill` | `0` | 首次运行回填条数；0 = 只建基线不推送 |
| `body_max_chars` | `0` | **纯文字**通知的正文截断长度，默认 0 = 不截断（长文按 `chunk` 分段）。**有图片的通知恒不截断**（否则截断点之后的图片会被丢掉）|
| `images` | `{enabled:true, max_bytes:10485760, prefetch:true, prefetch_max:30, recrawl:true, recrawl_max:50, retry:2, retry_delay:2.0, max_attempts:5}` | **正文图片**：抓正文时记录每张图及其在正文中的位置，`get` 时按原排版插入。`max_bytes` = 单张上限（超限跳过）；`prefetch` = 同步时补齐图片缓存（本轮新通知优先，随后是**所有保留记录**里仍缺的图），`prefetch_max` = 单轮下载上限；`recrawl` = 给「没有 `images` 字段」的历史记录重抓正文页补图（每轮 `recrawl_max` 条，0 = 不限，都没补完的下一轮继续）；`retry`/`retry_delay` = 下载失败后**立刻重试**的次数与起始间隔（指数递增），`max_attempts` = 累计失败多少次后停止自动重试（失败次数记在 `data/image_failures.json`，命令可强制重试）；`enabled:false` 退化为纯文本 |
| `tables` | `{enabled:true, max_per_notice:5}` | **正文表格 → 图片**：门户正文里的表格被提取器**拍平成文本**（每个单元格一行，行列关系全丢），所以表格单独用 Chromium 渲染成 PNG 发送。`max_per_notice` = 单条最多渲染几个表（按原文顺序取前 N 个）；`enabled:false` 或不装 Chromium 时退回文本形式。图片按**表格内容哈希**缓存在 `data/tables/`，同表只渲染一次 |
| `chunk` | `{max_chars:1200, max_images:4}` | **长消息分段**：单条消息超过该配额就拆成多条按序发送（`get` / 推送 / 早报都适用）。0 = 该维度不限 |
| ~~`binding`~~ | — | **已合并进 `targets`**（绑定即准入）。旧键仍兼容读取，但新逻辑只写 `targets` |
| `username` | — | 门户账号（学号/工号）|
| `password` | `""` | 门户密码（建议改用环境变量）|
| ~~`push_body`~~ | — | **已退役**：推送不再携带正文，正文恒被抓取落库以供 `get` 查询。键名保留但被忽略 |

## 3.2 凭据优先级

```
门户：  环境变量 BUPT_PORTAL_USERNAME / BUPT_PORTAL_PASSWORD   >   config.json
摘要：  环境变量 DEEPSEEK_API_KEY                              >   summarize.api_key
```

推荐用环境变量，避免密钥落到 `config.json`：

```bash
cp .env.example .env      # 填 BUPT_PORTAL_PASSWORD 与 DEEPSEEK_API_KEY
./run.sh
```

> 本机的 DeepSeek key 可直接从 DSH 凭据库取：
> `grep DEEPSEEK_API_KEY ~/.dsh/.credentials.yaml`

`plugins.bupt_news/data/cookies.json`（权限 `0600`）用于跨重启复用登录态；请勿提交或外传。

---
