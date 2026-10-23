# 六、工作流程

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 上一章：[作用域（命令能在哪里用 / 影响什么）](05-scope.md) · 下一章：[已知限制](07-limitations.md)


## 6.1 同步与推送流程

```
每 interval 秒 ─▶ fetch_list(每个 column)          抓第 1 页（最新 20 条）
              ─▶ 与 seen 比对，得新增 fresh
              ─▶ 首次运行？ → 只建基线，不推送
              ─▶ 对 fresh 抓正文（同时抽出正文图片及其位置）、分配 ID、
                 写 notices.jsonl（记录含 key、body、images[]）、标记 seen
              ─▶ 历史记录补图：重抓无 images 字段的老记录（images.recrawl/recrawl_max）
              ─▶ 补齐图片缓存：本轮新通知 → 所有保留记录缺失的图（images.prefetch/max）
              ─▶ 清理不再被引用的图片缓存
              ─▶ 在免打扰窗口内？ → 结束（只落库不推送）
              ─▶ pending = notices.jsonl 中「已落库但 key 不在 pushed」的全部记录
              ─▶ 无 pending 且今天早报已发？ → 结束
              ─▶ 非早报时按 max_push_per_round 截取最新 N 条
              ─▶ 批量摘要（DeepSeek，每 batch_size 条一次调用）
              ─▶ 组装消息：今天首次越过窗口？ → 早报（天气 + 列表）
                            否则             → 普通列表
              ─▶ 发送到 groups + users ─▶ 标记 pushed
```

要点：

- **去重**用全局桶 `_all`，键为 `treeid:newsid`，所以同一通知即便出现在多个栏目也只推一次。
- **积压（backlog）**是核心概念：`pending` 从「已落库但未 pushed 的记录」推导，
  **不是**从 `fresh` 推导。因为免打扰期间通知已被标记 `seen`，下一轮 `fresh` 不再包含它们
  —— 若从 `fresh` 推导，积压会被静默丢弃。为此通知记录持久化了 `key` 字段做对账。
- `enabled=false`（推送总开关）与免打扰**共用同一套积压机制**：关闭期间落库不推送，
  重新开启后一并补发。
- **推送顺序**为旧 → 新。
- **正文图片**：抓正文时同时记录每张图及其在正文中的字符偏移（记录里的 `images: [{pos, url}]`），
  `/buptnews get` 时按偏移把正文切成多段、图片插回原位置，消息里的图文顺序与网页一致。
  图片带门户 Cookie 下载到 `data/images/`（同一 URL 只下一次）：**同步时补齐缓存** ——
  本轮新通知优先，随后按记录从新到旧补**所有保留记录**里仍缺的图（每轮上限 `prefetch_max`，
  没补完的下一轮继续，日志会报剩余张数）；仍未下到的（例如本轮超限）在 `get` 时懒加载补齐。
  每次同步后按「仍被保留记录引用」清理缓存。**推送/早报不带图片**（只有摘要行）。NapCat 侧**不需要**能访问门户、也**不需要**携带 Cookie；`get` 遇到没有 `images` 字段的历史记录时会在消息里提示「尚未补图」并给出 `/buptnews images` 入口。
- **图片下载失败会重试**：失败后立刻再试 `images.retry` 次（默认 2，间隔 `retry_delay × 2^k`
  指数递增）；失败账本在 `data/image_failures.json`（记累计次数、最后错误与时间，成功即移除），
  累计到 `images.max_attempts`（默认 5）后自动补齐不再空撞，可用 `/buptnews images retry [数量]`
  强制重试（忽略次数上限）。
- **历史记录补图**：`images` 字段是后加的，之前入库的记录只有正文。同步时会重抓这些记录的
  正文页补出图片位置（每轮 `recrawl_max` 条，默认 50），补完记录数归零后这一步自动变成空操作；
  想一次多补些可用 `/buptnews images backfill 200`，或在停掉 bot 后用
  `venv-arm/bin/python tests/backfill_images.py`（一次性补齐，自动备份 notices.jsonl）。
- **长消息分段**：`get` 的图文消息、以及推送/早报，超过 `chunk`（默认 1200 字 / 4 张图）
  就拆成多条消息**按原顺序**发送；文字优先在换行处断开，图片/媒体段不会被拆开。
- **消息格式**：每行 `ID 摘要`（20 字以内，ID 前不带 #）。不带部门/日期/链接——要细节用 `get`。
- **早报**每天一次：首行天气，其后为积压列表；当天无积压则**只发天气行**。
  机器人若在当天下午首次启动，那次同样算早报（因而带天气行）。
- **带伞提醒**：天气行在需要带伞时追加「 ☂️ 记得带伞」。判据（满足其一）：
  ① 天气码属于降水类（雨/阵雨/雷雨/冻雨/冰雹/雪）；
  ② 降水概率 ≥ `weather.umbrella_pop`（默认 50%）。
  加②是因为 `weather_code` 是当天**主导**天气，单看它可能漏掉「白天阴、傍晚转雨」。
  取不到天气时（降级为「天气暂不可用」）**不会**误报带伞。
- **首次运行**默认只把现有通知记为基线并打印日志（`first_run_backfill: 0`）。
- 会话失效（被重定向回 CAS）时自动重新登录；DNS/连接抖动会重试 3 次。
- **推送耗时含上报节流**：框架对每个 API 上报加了 `api_delay`（默认 0.5–1.0s，见
  `config.json`），所以「推送 N 个目标」实际耗时约 `N × 0.75s`（8 个目标 ≈ 6s），
  这是刻意换取的防风控余量（见 (../../guide/02-client.md#24-断线重连与上报节流)）；压测时可把 `api_delay` 设为 `0`。
- **掉线期间的调用会排队**：NapCat 未上线时发送调用排队等待（`offline_hold_timeout` 默认 `0`
  = 一直等，`offline_queue_max` 默认 1000），重连后按序补发；**已发出但没等到响应**的调用
  仍立即失败、不自动重发，避免消息重复（见 (../../guide/02-client.md#24-断线重连与上报节流)）。
- **小窗命令按来源回复**：群聊临时会话（小窗）的回复会自动带回来源群
  （`MessageEvent.is_temp_session`），不会发成好友私聊；`preview` 与用法错误提示同样按来源回
  （见 (../../guide/06-commands.md#622-群聊临时会话小窗)）。

## 6.2 正文表格渲染为图片

门户正文里的表格如果直接取文本，会变成「时间 / 10月10日 / 10月18日 / 9:00-9:30 /
经济管理学院 / …」这样一串单元格，**行列对应关系彻底丢失** —— 看不出哪个学院属于哪个时段。
所以表格必须渲染成图片。

链路（与图片共用同一套 `pos` 偏移机制）：

```
portal.parse_detail()
  ├─ images: [{pos, url}]        图片
  └─ images: [{pos, table: 0}]   表格 → 指向 tables[0] 的原始 HTML
     tables: ["<table>…</table>"]     表格原文（必须存：正文文本已拍平）
        ↓  发送时
  _ensure_table_image(i, tables)
     ├─ parse_table()   解析成 rows×cols 网格（处理 rowspan/colspan，占位格跳过）
     ├─ is_renderable() 过滤噪声表（"打印"工具栏表、空布局表、极短表单）
     └─ TableImageStore.ensure(html)  → Chromium 渲染 → PNG（按内容哈希缓存）
        ↓
  MessageChain 里按 pos 插入图片段（与正文文字同一顺序）
```

渲染细节：

- 用 **Chromium headless** 渲染，原文的边框/对齐/`rowspan` 天然正确；
  两趟：`--dump-dom` 量出表格实际尺寸（脚本把尺寸写进 `<title>`）→ 按该尺寸
  `--screenshot` → 再按非白像素 bbox 裁剪（去掉浏览器默认外边距）。
- 渲染前**剥掉会联网的东西**（`<script>`、`<link>`、`@import`、外链图片）：
  离线环境里等这些资源会把截图挂住。
- 缓存文件名 = 表格 HTML 的 sha256 前 16 位，因此同表不重复渲染、内容变了自动换图；
  不再被任何记录引用的图片会随 `_prune_images()` 一起清掉。

历史记录（表格功能上线前入库的）会在同步时**自动重抓正文页补表格**，与补图共用一条
`recrawl` 通道（每轮 `images.recrawl_max` 条，从最旧记录开始）。日志会分别报
「无图 / 无表」的条数：

```
[bupt_news] 历史记录补素材：重抓 50 条（无图 36 条 / 无表 48 条 / 失败 0 条），剩余 386 条
```

想**立刻**补某一条（不必等批量轮到它）：

```
/buptnews images backfill id=476      # 只重抓 #476，刷新正文 + 图片 + 表格
/buptnews get 476                     # 表格按需渲染成图片（首次约 2s，之后走缓存）
/buptnews images backfill 0           # 批量补抓全部剩余（0=不限，约 1 秒/条）
```

> 表格图片是**按需渲染**的：`recrawl` 只负责把表格原文抓进库，
> 真正出图发生在推送或 `/buptnews get` 时；同一张表只渲染一次。

## 6.3 @全体成员

群推送在发送前**先调 `get_group_at_all_remain` 查询**该群「能否 @全体成员」以及
「还剩几次」，再据此决定行为：

| 查询结果 | 行为 |
|----------|------|
| `can_at_all: true` | 第一条消息前加 `@全体成员`，其余分段正常发送 |
| `can_at_all: false` | 不 @all，并在消息后**补一条醒目提示**提醒同学注意查收 |
| 查不到（接口失败 / 字段不认识 / `at_all: false`） | **既不 @all 也不提示** —— 误判成「用尽」会导致每轮都刷一条提示 |
| — | **只对群聊生效**：私聊原文照发，绝不带 `@全体成员`（带了会显示成一行文字） |

判定统一看服务端给的 `can_at_all`，剩余次数（`remain_at_all_count_for_group` /
`remain_at_all_count_for_uin`）只用于日志展示：每次查询都会记一行

```
[bupt_news] 群 100000003 @全体成员：可用（本群剩余 5 次，账号剩余 20 次）（原始返回 {...}）
```

推送成功后再记一行结论：

```
[bupt_news] 已推送 → 群 100000003（@全体成员=是，可用（本群剩余 3 次，账号剩余 18 次））
```

提示文案：

```
⚠️ 本条为重要通知 ⚠️
本群 @全体成员 次数已用尽，无法再提醒所有人。
请同学们留意本群消息，避免错过通知 🙏
```

实现上要注意：`MessageChain` 是**可变**对象（`.at()`/`.text()` 往同一个对象追加），
所以每个群都要用**独立的分段列表**（`chunks_for_group = list(chunks)`），加前缀只作用在
该群那份上，否则同一群的前缀会被反复叠加。

配置：

```json
"plugins": { "bupt_news": { "at_all": true } }
```

> 各家 OneBot 实现的字段名不完全一致，`_parse_at_all_remain()` 按
> `can_at_all` / `remain_at_all_count_for_group` / `remain_at_all_count_for_uin`
> 等已知字段解析，结果封装成 `_AtAllQuota`（`can_at_all` / `group_remain` /
> `uin_remain` / `exhausted` / `summary`）；完全认不出来时 `can_at_all=None`，
> 即「不知道」，既不 @all 也不提示。

## 6.4 学生通知判定（不 @全体成员）

AI 摘要在生成摘要的同时判定该通知**是否面向学生**（本科生/研究生）：

| 判定 | 例子 | 效果 |
|------|------|------|
| `true`（学生） | 选课、考试、竞赛报名、奖助学金、学籍、宿舍、学生活动 | 照常推送，照常 @全体成员 |
| `false`（非学生） | 教师、行政、部门、机构编制、干部任免、资产/招标 | 照常推送，但**不 @全体成员**，并加提示 |

提示文案（与 @全体成员 一起收敛：既然不是给学生的，@全体成员只会打扰全群）：

```
⚠️ 疑似不是给学生的通知
```

- **列表/早报**：本批通知**全部**判定为非学生时才加提示（混合批次不加，避免误导）；
- **单条详情**（`/buptnews get`）非学生时多一行提示，`/buptnews list` 里带 `⚠️非学生` 标记；
- **拿不准算学生**：`student_ai` 缺失（AI 没返回判定）时按「发给学生」处理 —— 宁可多 @
  一次全体成员，也不要漏掉真正发给学生的通知。

判定与摘要一起落库（`for_students` + `student_ai`）。缺判定同样计入「待补试」，
但走**只问判定**的轻量提示词（`STUDENT_ONLY_PROMPT`）：标题就够，不重新生成摘要 ——
省 token，也不会把已经发出去的摘要改写成另一个版本。

> ⚠️ 解析器必须与提示词配套：只问判定的回复**没有 `s` 字段**，用 `parse_digests`
> 会把整条丢掉。`_one_batch` 因此接受 `parse` 参数，且**有内容却解析不出条目时会显式报错**
> （把响应的 `finish_reason` 与前 120 字打进日志），不会静默失败。

> 两侧提示词都强调「拿不准给 true」，所以判定偏保守：**只有模型明确说不是学生**才会
> 抑制 @全体成员。

---

## 7.6 置顶与搜索

**置顶**：把重要通知钉在 `/buptnews list` 顶部，**每页都显示**（置顶的意义就是不用翻页）。

```
/buptnews pin <编号>          # 置顶（可多个：pin 12 34，也支持 12,34）
/buptnews pin                 # 看当前置顶
/buptnews unpin <编号>        # 取消置顶
/buptnews unpin               # 清空全部
```

持久化在 `data/state.json` 的 `pins` 字段（id 列表，按置顶顺序，最近的在后）：

- 置顶/取消**立即落盘**（`store.save()` 原子替换），重启、插件重载、进程崩溃后都还在；
- 放在 `state.json` 而不是 `notices.jsonl`：置顶是"用户选择"这类小状态，
  且记录被 `notice_retention` 淘汰后，它的置顶标记会被 `prune_pins()` 一并清掉，
  不会留下"显示已置顶但查不到正文"的幽灵条目；
- 置顶只是**展示排序**，不改变推送/早报的选取逻辑。

**搜索**：按关键词在标题、AI 摘要、正文、发布单位、通知编号里做**子串**匹配
（不区分大小写；中文没有词边界，分词反而会漏）。

```
/buptnews search 疫苗
/buptnews search 疫苗 10 2        # 每页 10 条，第 2 页
/buptnews search 疫苗 10 1 2026-10-08   # 只看某天
```

结果按最新在前，每行是 `📌 编号 [摘要] 标题`：

- **标题一定显示**：摘要只有 20 字，命中常在标题里（"疫苗补种通知"），
  只给摘要看不出为什么这条被搜出来；
- 命中在正文里时补一行上下文（`└ …`），命中在标题/摘要里则不重复；
- 已置顶的条目在结果里带 `📌`。

访问边界与 `/buptnews get` 一致（仅私聊）：搜索能翻出正文，属于比列表更高的信息暴露。

### 三种编号

| 编号 | 形态 | 来源 | 用法 |
|------|------|------|------|
| 本地编号 | `476` | 入库时分配（纯数字，只增） | `get 476` · `pin 476` |
| 门户编号 | `E1759-142679` | 门户检索结果（**可解码**） | `get E1759-142679` · `pin E1759-142679` |
| 置顶编号 | `P2` | 置顶时分配（独立空间） | `get P2` · `unpin P2` |

**门户编号自带信息**：`E<treeid>-<newsid>`，而门户链接正是
`xntz_content.jsp?...&wbtreeid=<treeid>&wbnewsid=<newsid>`，
所以**从编号就能直接还原原文链接**（`decode_external()` / `external_url()`），
不依赖任何映射表 —— 缓存被清理后编号照样可用（最多重新抓一次详情）。
缓存（`state.json` 的 `externals`）只是顺手存标题/日期，省一次抓取。

**置顶编号独立于通知编号**：`P1/P2…` 按置顶顺序递增，取消后空号可被下一次置顶复用，
所以列表始终紧凑。`pin` 返回分配到的编号，`unpin P2` 摘的是"第 2 个置顶项"，
不是 `id=2` 的通知。外部结果的置顶会**快照整条通知**（正文/图片/表格），
因此 `get P2` 不联网也能看全文。

**E 编号置顶会自动补 AI 摘要**：门户结果本来没有摘要（本地库没它、同步也不会抓它），
所以置顶时把正文抓下来后**顺手生成一条摘要**，写进置顶快照并落盘：

```
/buptnews pin E1196-45767   → 📌 已置顶 1 条：P1（E1196-45767）
                              P1 📌 2017级研究生注射麻腮风疫苗
/buptnews get P1            → 🤖 2017级研究生注射麻腮风疫苗
```

- 置顶列表**每行只有 `P编号 + 概要`**：不显示 E 编号/日期/本地通知编号 ——
  置顶区是给人快速扫的，编号混在一行里只会把概要挤掉（需要操作时再用 `get P<n>`）；
- **置顶有条数上限**（`pin_limit`，默认 **3**）：满了再 `pin` 会**拒绝**并提示
  `先 /buptnews unpin P<n> 腾位置，或加 --force`；加 `--force`（或 `-f`）才替换
  **最早置顶**的那条（FIFO），新项继承它的 P 编号，列表始终紧凑：

  ```
  /buptnews pin 4            → ⚠️ 置顶已满（上限 3 条），未置顶：4
  /buptnews pin 4 --force    → 📌 已置顶 1 条：P1（#4）
                               ♻️ 已替换最早置顶的：P1 📌 旧概要
  ```

  淘汰顺序看**置顶先后**而不是 P 编号 —— P 编号会复用，P1 可能反而最新；
- 摘要失败（AI 不可用）时**不写假摘要**（不拿标题截断冒充），置顶仍然成功；
- 之后查看 `pin` 列表 / `get P<n>` 时会**再补一次**，补上立刻显示并写回快照；
- 写回走 `store.update_external_notice()`，同时更新 `externals` 缓存与置顶快照
  （改的是 `_data["pins"]` 的原始 dict，不是 `pinned_entries()` 返回的副本）。

### 门户检索（本地库里没有的旧通知也能搜到）

本地库只保留最近 `notice_retention`（默认 500）条，门户里却有**多年历史**。
`search` 默认同时查两处（`--source=all`）：

```
/buptnews search 麻腮风                    # 本地 + 门户
/buptnews search 麻腮风 --source=local     # 只搜本地库
/buptnews search 麻腮风 --source=portal    # 只搜门户（含 2017 年的旧通知）
/buptnews search 麻腮风 --content=false    # 门户只搜标题（默认连正文一起搜）
```

输出分两段：

```
📚 本地库（可 /buptnews get <编号> 看全文）
   476 [2026级新生接种麻腮风乙脑疫苗] 工作通知丨关于2026级大一进京新生接种麻腮风疫苗和乙脑疫苗的通知
🌐 信息门户（含本地库里没有的旧通知）
   #476 2026-10-08 工作通知丨关于2026级大一进京新生接种麻腮风疫苗和乙脑疫苗的通知
      2025-12-02 工作通知 | 关于2025级大一新生接种麻腮风疫苗和乙脑疫苗的通知
     └ /buptnews get http://my.bupt.edu.cn/xntz_content.jsp?...&wbnewsid=133322
```

- 门户结果**已在本地库**的会标本地编号（可直接 `get 编号`）；不在库里的给 **E 编号**，
  `get E1759-142679` 直接看全文（图片/表格也走同一套渲染），也能 `pin` 它；
- 门户检索协议（`fz_ssjg.jsp`）是逆向页面的，改动前先核对：
  关键词走 **POST + `INTEXT2`（base64）**，`wbnewsfield` 选 `wbtitle`/`wbcontent`；
  `wbtreeids` 必须**从列表页表单里读**（写死会漏栏目）；翻页不能用 POST，
  必须 GET `?a218493i=<kw>&a218493c=<每页>&a218493p=<页码>`；
- **结果缓存 5 分钟**（同关键词/页码/参数），避免"搜一次→翻页→返回"反复打门户；
- 门户故障时**降级为只显示本地结果**并明确提示 —— 搜索不该因为门户抖动整体失败。

> 字段顺序坑：可选参数按**字段顺序**绑定位置参数。
> `SearchSchema` 里 `source`/`content` 必须排在 `limit`/`page`/`date` **之后**
> （否则 `search 疫苗 5 1` 的 `5` 被当成 `source`）；
> `PinSchema` 里 `ids` 必须排在 `force` **之前**（否则 `pin 4` 的 `4` 被绑到 force）。
>
> 另一坑：框架解析器只认 `--key=value`，**裸 `--force` 会被当成位置参数**。
> 所以 `pin` 从 `packet.raw` 里额外识别裸 `--force` / `-f`，并把它们从 `ids` 里剔除。

---

## 7.7 第二课堂活动同步

在通知之外，可选地把「北邮第二课堂」的活动也同步进来。协议见
`plugins/bupt_news/afterclass/API.md`（由小程序产物逆向整理）。

**为什么不能自动登录**：`POST /api/v1/auth/sessions` 在需要人机校验时返回
419/420，验证码页 `/mp-captcha.html` 是商业反爬控件（瑞数 `$_ts` 混淆）。
绕过它属于规避访问控制，所以本实现**只接受你自己提供的 JWT**，不实现登录。

取得 token（网页版，推荐）：

1. 浏览器打开 <https://dekt.bupt.edu.cn/>，用学号密码登录（验证码由你自己输入）；
2. `F12` → Console → 执行：

   ```js
   localStorage["secondclass.tokenv3"]
   ```

   打印出来的 `"eyJ..."` 就是 JWT（连引号一起复制也行，程序会清洗）；
   也可以走 Application → Local Storage → 键 `secondclass.tokenv3`。

小程序里则是 Storage 的 `tokenv3` 键（值相同）。
JWT 的 `exp` 是过期时间 —— 过期后重新登录再取一份覆盖即可。

放置位置（二选一）：

```bash
# ① 文件（推荐；插件目录下，惰性读取）
echo '<JWT>' > plugins/bupt_news/afterclass/token.txt
# ② 环境变量
export DEKT_TOKEN='<JWT>'
```

先用探针确认能拉到数据：

```bash
./venv-arm/bin/python -B tools/dekt_probe.py          # 打印归一化后的前 10 条
./venv-arm/bin/python -B tools/dekt_probe.py --json   # 附带原始字段名
```

确认无碍后开启（`"afterclass": {"enabled": true}`），再**用命令绑定**：

```
/buptnews add group 100000003 event=on      # 绑定并开启活动推送
/buptnews dektoken group 100000003 <JWT>    # 群主/管理员设置该群 token
/buptnews add user 10000001 event=on        # 绑定用户
/buptnews dektoken user <JWT>               # 该用户设置自己的 token
/buptnews set user 10000001 message=off     # 只改开关
```

**独立绑定**：一项 = **一个推送目标 + 它自己的 JWT**。

- 绑定存 `targets`；token 默认落**数据目录** `data/dekt-tokens/<group|user>-<号>.txt`
  （权限 `0600`，不写进源码目录）；
- 每个绑定**单独**去重、单独存活动（`data/activities/<group|user>-<号>.json`）、
  单独计单轮上限、单独容忍 token 过期 —— 一个坏掉不影响其它；
- 活动**只发给该绑定的目标**，不走通知的目标列表，也**绝不 @全体成员**；
- `dektoken` 写完**立即生效**（重建绑定 + 重读 token），不用重启。

为什么必须各自一份 JWT：`/api/v1/activity` 的返回是**按账号**算的
（`college_id` / `class_id` / `grade` / `role_id` 都来自 token 对应的人）。
同一份 token 只能代表一个同学看到的活动，硬发给别的群既不准也不合适。

**真实响应字段**（2026-10 实测，`tools/dekt_probe.py --json` 可复查）：

```
id, name, location, area, activity_class, demands, attend_limit, customer,
activity_start_time, activity_end_time, signup_start_time, signup_end_time,
belong_to, target_id, target_role, grade, priority, picture_url, digest
```

- 时间是 **ISO8601 UTC**（`2026-10-26T07:00:00Z`），渲染时转本机时区（CST/UTC+8）；
- `signup_*_time` 常用 `1970-01-01T00:00:00Z` 表示"没有报名窗口"，
  这种哨兵值**不显示**（否则会写成"报名 1970-01-01 08:00"）；
- `activity_class` 要用 `/activity/class` 的 `id → name` 翻译（"文艺活动"/"学术讲座"…）；
- `demands` 是位掩码（`23` = 报名+签到+签退+管理）。

**请求指纹锁死成微信客户端**：服务端只认 `MicroMessenger` 客户端的 UA，
换成桌面 Chrome / `curl` / 或"Linux + MicroMessenger"这种自相矛盾的组合会被
WAF 挡掉。`dekt.py` 里固定成一台 iPhone 上微信小程序的 UA，**不要按平台改、
也不要随机轮换** —— 换一次等于换一台设备，反而更可疑。

**刷新节奏**：活动**不跟通知的 10 分钟一轮**，而是自己在
`interval_min`~`interval_max`（默认 600~3600 秒，即 **10–60 分钟**）之间
每次取随机值：

- 服务端活动本身的更新节奏就是 10–60 分钟不等，刷得更勤只增加被风控的机会；
- 固定周期（比如整 10 分钟）是最容易被识别成机器人的特征之一，所以加抖动；
- 下次刷新时间落在 `state.json` 的 `meta.acts_next_at`，**热重载/重启不会把它清零**
  （否则每次改代码都会立刻多刷一轮）；
- 手动 `/buptnews sync` 视为"现在就看"，**强制**刷一次，不等间隔；
- `/buptnews status` 里能看到 `下次刷新 HH:MM`。

**同步口径**（与通知刻意保持一致）：

- 挂在同一轮定时同步之后（`_sync_once` → `_do_sync` 之后），
  通知失败不影响活动，活动失败也不让整轮算失败；
- **首轮只建基线**：第一次拉到的是存量活动，全部标成"已推送"而不发，
  否则一开启就把历史活动全推一遍；
- **免打扰**：窗口内**连访问都不做**（不只是不推送）—— 半夜还在定时打第二课堂接口，
  本身就是异常行为，所以直接跳过整轮拉取，也不推进"下次刷新时间"，醒来后第一轮立刻补上；
  通知那条链路的免打扰仍然是"只入库不推送"，两者口径不同是有意的；
- **单轮上限** `max_per_round`：一次最多推几条，其余留到下一轮；
- 同一活动重复拉到时**就地刷新**（时间/地点可能被改），不会重复推送；
- 推送目标是**每个绑定自己的那一个**（不再复用通知的 `groups`/`users`）；
- **不 @全体成员**：活动是常规招募，每来一条就 @全群会把人吵到退群。

**看活动**：`list` 默认只给置顶+通知，活动要看才拿：

```
/buptnews list                    # 置顶 + 通知（默认）
/buptnews list --scope=acts       # 只看第二课堂活动（-a 是简写）
/buptnews list --scope=all        # 通知 + 活动
```

活动区按时间倒序、多绑定时标出归属（`[某班] 13452 10-26 15:00 …`），
只显示最新 `list_page_size` 条并说明总数。

**续期不用重启**：`dektoken` 写完立即生效；也可以直接覆盖对应 token 文件，
每轮同步都会重读（内容变了就重建会话）。
JWT 的 `exp` 会被解出来，**剩余不足 3 天开始每天提醒一次**，过期后日志给出具体动作
（`/buptnews status` 也能看到 `token 至 YYYY-MM-DD`）。

> 自动化边界：**登录这一下无法自动化**。实测在真实 Chromium 页面里调登录接口仍返回
> `420`，并加载 `/captsec` 的人机校验 iframe（商业反爬）。绕过它属于规避访问控制，
> 所以流程是"你登录一次 → 复制 token → 其余全自动"。

状态可查：`/buptnews status` 里有 `🎪 第二课堂：N 条活动 / M 条待推`。
token 失效时日志提示 `token 已失效，跳过活动同步`，同步其余部分照常。

---
