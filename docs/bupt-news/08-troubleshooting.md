# 八、排障

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 上一章：[已知限制](07-limitations.md) · 下一章：[与 Node 爬虫的关系](09-node-crawler.md)


## 8.1 WebSocket 连不上：先看异常类型

`websockets` 的异常类型就能定位病因（**本机实测**，非推测）：

| `ws_url` | 对端实际是什么 | 抛出的异常 |
|---|---|---|
| `ws://host:3001` | 明文 WebSocket ✅ | 正常连接 |
| `ws://host:<TLS 端口>` | HTTPS / WSS | **`InvalidMessage: did not receive a valid HTTP response`** |
| `wss://host:3001` | 明文 WebSocket | `SSLError: RECORD_LAYER_FAILURE` |
| `ws://host:6099` | NapCat WebUI（明文 HTTP）| `InvalidStatus: server rejected WebSocket connection: HTTP 401` |
| `ws://host:<无监听>` | —— | `ConnectionRefusedError` |

> ### ⭐ `InvalidMessage: did not receive a valid HTTP response`
>
> **几乎总是「用 `ws://` 连了一个 TLS 端口」。**
>
> 明文 HTTP 握手请求发到 TLS 服务端，对方回的是 TLS 记录（二进制），
> WebSocket 客户端的 HTTP 解析器读不出来，于是报这个错。
> 反方向写错（`wss://` 连明文端口）报的是 `SSLError`，**两者不会混淆**，
> 所以看到 `InvalidMessage` 就可以直接去查 scheme / 端口是否指向了 HTTPS/WSS。

## 8.2 正确配置

NapCat 的 WS 服务端（`/opt/ncqq/napcat/config/onebot11_<QQ>.json`）：

```json
"websocketServers": [
  { "enable": true, "host": "127.0.0.1", "port": 3001, "token": "..." }
]
```

→ ncqqpy 的 `config.json` 必须对应为：

```json
"ws_url": "ws://localhost:3001",
"token":  "（与 NapCat 的 token 一致）"
```

scheme 是 **`ws`**，不是 `wss`。

## 8.3 三步自查

```bash
# 1. 端口在听吗（应看到 127.0.0.1:3001）
ss -ltn | grep 3001

# 2. 它是明文 WS 服务端吗（应返回 426 Upgrade Required）
curl -i http://127.0.0.1:3001/

# 3. 直接试连
venv-arm/bin/python -c "
import asyncio, websockets
async def m():
    ws = await websockets.connect('ws://127.0.0.1:3001'); print('连接 OK'); await ws.close()
asyncio.run(m())"
```

---

## 8.4 摘要服务（DeepSeek）的三个坑

> 端点由 `summarize.base_url` / `model` / `api_key` 决定，**环境变量只覆盖 key**。
> 校内网关与官方 API 都兼容 OpenAI `/chat/completions`，换端点只改这三个值即可。
> `import_history.py --summarize` 与插件读的是**同一份** `config.json`，不会各走各的。

**1. 校内网关有 30 次/窗口 的请求限额，且只放行白名单模型**

实测（`https://myai.bupt.edu.cn/llm-gw/v1`）：

| 现象 | 说明 |
|---|---|
| 超额返回 **HTTP 429** | `Rate limit exceeded for user: cas_xxx. Current limit: 30`，响应里带 `Limit resets at` |
| 换模型名返回 **403** | `key not allowed to access model. This key can only access models=['deepseek-v4-flash']` |
| 上游故障时返回 **HTTP 500** | `rrgw.InternalServerError: upstream - Internal Server Error` |

⚠️ **调用次数 = 待摘要条数 ÷ `batch_size`**：415 条按 `batch_size=10` 算是 42 次 > 30，
一次跑必然超额。给校内网关做批量摘要时把 `batch_size` 调大（如 20 → 21 次），
或分多次运行（工具只处理「尚无摘要」的记录，可以安全续跑）。

**2. `max_tokens` 给小了会「静默返回空摘要」**

`deepseek-flash` 是**推理模型**，reasoning 占约 93% 的 completion token。实测：

| `max_tokens` | 结果 |
|---|---|
| 64 | ❌ `finish_reason=length`，`content` 为**空串** |
| 1024 | ✅ 单条可用（约耗 291 token，其中 reasoning 282）|
| 3072 | ✅ 6 条批量可用（约耗 1551 token，其中 reasoning 1436）|

所以 `summarize.max_tokens` 默认给到 4096，且 `_one_batch` 遇到空 `content` 会**显式抛错**
而不是返回空摘要——否则会静默推送空消息。

**3. 思考链可以关掉，而且应该关掉（`reasoning_effort`）**

摘要很短，思考链纯属浪费。实测同一 prompt 的四种写法：

| 写法 | reasoning_tokens | completion_tokens |
|---|---|---|
| 不传（默认） | 761 | 771 |
| `reasoning_effort: "low"` | 454 | 466 |
| **`reasoning_effort: "none"`** | **0** | **10** |
| `enable_thinking: false` | 465 | 475（**无效**）|
| `thinking: {"type":"disabled"}` | 0 | 9（**同样有效**）|
| `chat_template_kwargs: {"thinking": false}` | 706 | 718（**无效**）|

批量 5 条的实际对比：

| 设置 | 耗时 | 结果 |
|---|---|---|
| `reasoning_effort: "none"` | **1.0s** | 5/5，平均 14 字 |
| `reasoning_effort: "low"` | 6.6s | 5/5，平均 16 字 |

→ **默认配置为 `summarize.reasoning_effort = "none"`**（快约 6.6 倍，质量相当）。
想恢复思考链就把它设成 `low`/`high`/`max`，或设成 `"default"`/`"off"` 让请求不带该字段。

> 注意 `"none"` 不在服务端公布的 `low/high/max` 列表里，但实测被接受；
> 若换到别的网关且报参数非法，去掉该键即可（`reasoning_effort: "default"`）。

## 8.5 AI 摘要重试与时间预算

摘要失败（限流 429、上游 5xx、超时、空 `content`、AI 少返回某几条）如果直接**静默降级**
成标题截断写进库里，之后就没有机会补上了（库里已经有值）。现在的链路是三层重试 +
失败可识别：

| 层 | 触发 | 重试次数 | 说明 |
|----|------|----------|------|
| ① 同步内连续重试 | 每轮同步给待推送记录摘要 | `retry`（默认 **5**） | 指数退避，间隔 `retry_delay`（默认 2s，逐次翻倍）|
| ② 下次同步自动补试 | 每轮同步末尾 | 同 ① | 扫「还没有 AI 摘要」的历史记录，单轮最多 `retry_scan_max`（默认 60）条 |
| ③ 调取时重试 | `/buptnews get <编号>` | `retry_on_notice`（默认 **2**） | 交互路径要快，所以次数比同步少；成功后写回库，`/buptnews list` 立刻能看到 |

补试（②）的三个工程约束，都踩过坑：

- **只能有一个调用点，且必须在所有提前 return 之前。** 放在 `return` 之后 →
  「上次 AI 失败、之后再没新通知」的记录永远等不到重试；调用两次 → 同一轮同步跑两遍补试，
  API 调用翻倍。
- **节流**：上一轮一条都没补成功时冷却 `retry_cooldown`（默认 1800s），否则每轮同步
  （间隔 10 分钟）都会对同一条失败记录重复烧 API。只要还有进展就继续下一批，不等待。
  注意冷却期内**不要**因为"待补恰好为空"而重置冷却（那会让冷却提前失效）。
- **补试不赶时间**：单请求超时用 `retry_timeout`（默认 120s，比 `summarize.timeout` 宽松）、
  批量用 `retry_batch_size`（默认 6，比 `summarize.batch_size` 小）——
  实测网关较慢时 10 条/批会 60s 超时，整批白丢。

> `_judge_only` 还有一条：**没有进展就立刻停** —— 失败的批次不做批次内的重复请求，
> 否则一次补试就会对同一批反复调用、每次都整批超时。

**时间预算**：三层都受**时间预算**约束，到点就带着已有结果先发，剩下的留给下一轮 ——
否则补试几十条历史记录会把当天的早报挡在后面：

| 阶段 | 预算配置 | 默认 | 说明 |
|------|----------|------|------|
| 本轮推送的摘要 | `summarize.push_budget` | 200s | 时间敏感：到点就用已有摘要渲染，缺的降级为标题截断；**必须 ≥ `summarize.timeout`**，否则单次尝试会被预算砍掉 |
| 后台补试 | `summarize.retry_budget` | 90s | 整轮补试的总预算 |
| 单次请求 | `summarize.retry_timeout` | 120s | 补试比推送宽松（后台任务不赶时间）|

**顺序**同样重要：补试是后台维护任务，**必须排在推送之后**（`_backfill_ai_results()`
统一入口，每轮最多调用一次）—— 放在推送前会直接顶掉当天早报。

预算的检查点有两处，缺一不可：**每次尝试之前**，以及**退避 sleep 之前**
（只查尝试前是不够的：累积退避会睡过 deadline，实测 2s 的预算仍拖出 25s）。

另外，DNS 解析失败 / 连接被拒 / 请求超时被判为"重试无意义"（`_is_hopeless_error()`），
立刻收工，不做指数退避。

关键约定（都靠记录里的 `summary_ai` 标记）：

- **只有 AI 真的成功才写库**（`summary` + `summary_ai: true`）。重试用尽时**不写降级摘要**，
  降级文本只用于本次渲染 —— 否则「库里有值」会让后续所有重试都失去意义；
- 已经拿到 AI 摘要的记录**不再重复调用 API**（不花冤枉钱）；
- 重试只针对**还缺的**条目：一批里成功几条就记下几条，后续尝试不再重问它们。
  早期实现每次尝试覆盖整份结果，于是「第一次已成功的那条」会在后面的失败尝试里被丢掉；
- AI 少返回某条也算失败（`missing` 非空即重试），否则那条会被静默跳过。

失败时的日志（便于确认到底为什么失败）既有每次尝试的告警，也有最终结论：

```text
[bupt_news] AI 摘要失败（同步，3/10 条缺失，第 1/6 次）：HTTP 429: rate limited；2.0s 后重试
[bupt_news] AI 摘要重试 6 次仍失败（同步，10 条）：HTTP 429: rate limited；本轮不写降级摘要，下次同步/调取时再试
```

---
