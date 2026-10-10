# bupt_news 插件 —— 校内通知同步 + 免打扰早报 + ID 检索 + DeepSeek 摘要

每 10 分钟同步一次北邮信息服务门户的「校内通知」，把**新增**通知推送到指定的 QQ 群 / QQ 用户。

- **免打扰**：默认 23:00–07:00 不推送，次日首次越过窗口时随**当天天气**汇总补发
- **短 ID**：每条通知分配 `#1` 递增编号，可用 `/buptnews get <编号>` 查回全文（**图文按原排版**）与原文链接（含附件入口）
- **20 字摘要**：推送只含 DeepSeek 生成的 ≤20 汉字梗概（批量调用；失败降级为标题截断）

设计规格见 `docs/specs/2026-09-24-bupt-news-digest-design.md`，
实施计划见 `docs/plans/2026-09-24-bupt-news-digest.md`。

---

## 一、目录结构

```
plugins/bupt_news/
├── __init__.py     # 导出 BuptNewsPlugin（框架按此发现插件）
├── main.py         # 插件主体：定时同步、免打扰/早报编排、推送、命令注册
├── portal.py       # 门户客户端：CAS 统一身份认证 + 列表/正文（含图片）抓取与解析
├── images.py       # 正文图片下载 + 本地缓存（按 URL 去重、自动清理）
├── backfill.py     # 图片补齐：历史记录重抓正文页 + 下载缺失图片
├── weather.py      # Open-Meteo 天气客户端 + WMO 天气码→中文映射
├── summarize.py    # DeepSeek（OpenAI 兼容）批量摘要客户端
├── schemas.py      # 命令参数 Schema
├── store.py        # 状态持久化：state.json（小状态）+ notices.jsonl（可检索正文）
└── data/           # 运行时生成：state.json、notices.jsonl、cookies.json、images/（图片缓存）
```

配套文件（不在插件目录内）——自检脚本、诊断探针与一次性维护脚本都在 **`tests/`** 下，仓库根只留运行入口（`main.py` / `run.sh` / `config.json` / `napcat/` / `plugins/`）：

| 文件 | 说明 |
|------|------|
| `tests/test_bupt_news.py` | 自检脚本（离线 **490+ 项**：解析 / 状态 / ID / 免打扰 / 早报 / 摘要 / 命令 / 权限与 `/help` / **私聊可用范围** / 图片排版 / 长消息分段 / WS 重连与上报节流；`--online` 追加在线联调）|
| `tests/diagnostics/probe_portal.py` | 在线诊断：分步验证 登录 → 列表 → 正文，带每步耗时 |
| `tests/diagnostics/probe_summarize.py` | 摘要接口调用形状探针（逐条 vs 批量、token 预算）|
| `tests/backfill_images.py` | 一次性补齐存量通知的正文图片（重抓 + 下载，自动备份 notices.jsonl）|
| `run.sh` | 启动脚本（自动用 `venv-arm`，并从 `.env` 读取密码）|
| `.env.example` | 环境变量模板（门户密码 + DeepSeek key）|

---

## 二、运行前提

1. **NapCatQQ 必须已运行**，并开启了 WebSocket 服务端，地址与 `config.json` 的
   `ws_url`（默认 `ws://localhost:3001`）一致。
   > `NapCatClient.start()` 会**先连 WebSocket，再加载插件**。所以 NapCat 没起来时，
   > 进程会在 `ws.connect()` 直接报错退出，插件不会被加载。
2. **Python 依赖**：使用本仓库的 `venv-arm`。
   > ⚠️ 原有的 `venv/` 是 **x86-64** 的（从别的机器拷来的），在 aarch64 上无法执行，
   > 且没有可执行位。请勿使用；用 `venv-arm`。

   重建方式：
   ```bash
   python3 -m venv --without-pip venv-arm
   venv-arm/bin/python get-pip.py
   venv-arm/bin/pip install websockets pydantic aiohttp aiofiles
   ```

3. **门户账号**（统一身份认证）——见下节。

---

## 三、配置

配置位于 `config.json` 的 `plugins.bupt_news`：

| 键 | 默认 | 说明 |
|----|------|------|
| `interval` | `600` | 同步间隔（秒），最小 60 |
| `enabled` | `true` | 推送总开关；`false` 时仍同步落库但不发送，**重新开启后会补发**（与免打扰同一套积压机制）|
| `columns` | `[1154]` | 栏目 `wbtreeid`，1154=校内通知，1305=公示公告，1221=校内新闻 … |
| `groups` | `[]` | 推送目标群号 |
| `users` | `[]` | 推送目标 QQ 号 |
| `quiet_hours` | `{start:"23:00", end:"07:00"}` | 免打扰窗口，支持跨午夜；同日窗口（如 01:00–06:00）也支持 |
| `weather` | `{enabled,label,latitude,longitude,timeout,umbrella_pop}` | Open-Meteo 天气（免 key）；`umbrella_pop` 默认 50 = 降水概率达 50% 就提醒带伞 |
| `summarize` | `{enabled,base_url,model,api_key,batch_size,max_tokens,timeout,reasoning_effort,retry,retry_delay,retry_on_notice,retry_scan_max}` | DeepSeek 摘要；`batch_size` 默认 10，`max_tokens` 默认 4096；失败重试见下节「AI 摘要重试」 |
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
| `binding` | `{groups:[], users:[]}` | **私聊可用范围**：谁能私聊（含群小窗）用命令。`groups` = 允许在**群聊临时会话（小窗）**里用命令的来源群，留空则沿用框架的 `bound_groups`（谁被监听就允许谁的小窗）；`users` = 允许**直接私聊**用命令的 QQ 号。两者都空 = 非超级用户私聊一律不服务（含 `get`）。群聊内不受影响；超级用户豁免。用 `/buptnews bind user\|group <号>` 增删，详见 §四之六 |
| `username` | — | 门户账号（学号/工号）|
| `password` | `""` | 门户密码（建议改用环境变量）|
| ~~`push_body`~~ | — | **已退役**：推送不再携带正文，正文恒被抓取落库以供 `get` 查询。键名保留但被忽略 |

### 凭据优先级

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

## 四、命令

命令前缀取自 `config.json` 的 `command_prefixes`（默认 `/` 和 `#`）。
下表按 `plugins/bupt_news/main.py` 的**实际注册声明**列出（`scope`/`permission` 以代码为准）：

| 命令 | 范围 | 权限 | 说明 |
|------|------|------|------|
| `/buptnews` | 私聊 / 群聊 | 无（命令组本体） | 帮助：打印全部子命令用法 |
| `/buptnews status` | 仅私聊 | **superuser** | 状态：开关、间隔、免打扰窗口、目标数、上次同步、上次早报、累计推送、可查询条数 |
| `/buptnews list [页码]` | 私聊 / 群聊 | 无 | 分页列出通知编号（最新在前，每页 `list_page_size` 条；标注 `第 x/y 页，共 N 条`）|
| `/buptnews get <编号>` | 仅私聊 | 无 | 查看通知全文：标题/部门/日期 + **正文图文按原页面排版** + 原文链接；**过长自动拆成多条消息**（图片带 Cookie 下载到本地缓存后发送）|
| `/buptnews targets` | 仅私聊 | **superuser** | 查看推送目标 |
| `/buptnews sync [页数] [preview]` | 私聊 / 群聊 | admin | 立即同步；`preview` 只私聊发给自己，用于验证链路 |
| `/buptnews add group\|user [号]` | 仅私聊 | **superuser** | 添加目标 |
| `/buptnews del group\|user [号]` | 仅私聊 | **superuser** | 移除目标 |
| `/buptnews on` / `/buptnews off` | 仅私聊 | **superuser** | 开关推送 |
| `/buptnews interval <时长>` | 仅私聊 | **superuser** | 设置间隔，支持 `600` / `10m` / `1h` |
| `/buptnews images [stats\|backfill\|download\|retry] [数量]` | 仅私聊 | **superuser** | 图片缓存：`stats`（默认）看缓存、待补数量与失败待重试；`backfill` 重抓历史记录补图并下载；`download` 只下载缺失的图；`retry` 重试之前下载失败的图（忽略次数上限）。数量 0 = 用配置上限（`recrawl_max` / `prefetch_max`）|
| `/buptnews bind [user\|group] [号] [add\|del\|list]` | 仅私聊 | **superuser** | 私聊可用范围白名单：加/删允许私聊（或开小窗）的用户与群，省略号数只看现状。**与 `add`（推送目标）是两件事**：那个管「往哪发」，这个管「谁能用」|

> 每条子命令都**显式声明** `permission=`：`permission=""`（`list` / `get`）表示显式覆盖、
> 无权限限制；父命令自己的 `permission="admin"` **只作为未声明时的默认值**（当前没有子命令
> 依赖它），且不拦截 `/buptnews` 本体。
> `add` / `del` 函数里那段「群里省略号码则用当前群」的分支当前不可达（子命令声明为仅私聊），
> 要加群目标请私聊机器人执行。

> ⚠️ `list` 的语义已调整：原先它显示「推送目标」，现在用于列出最近通知编号
> （因为早报文案引用了它）；原能力移到 `/buptnews targets`。

### 四之六、私聊可用范围（`binding`）—— 两道闸门，别混为一谈

私聊（`message_type="private"`，**含群聊临时会话即小窗**）的准入与「命令权限」是**两套独立**的判定，
先过范围、再过权限：

| 闸门 | 回答的问题 | 判定依据 | 不通过时 |
|------|-----------|---------|---------|
| **服务范围**（本节） | 这个**地址/人**能不能私聊用本插件 | `binding.groups` / `binding.users`（本插件自己的配置）| 静默丢弃（只记日志）|
| **权限**（`permission`） | 这个人**有没有资格**执行这条命令 | `superuser` / `admin` / `公开` + 框架角色缓存 | 回「未知命令」哨兵 |

准入规则（`plugins/bupt_news/main.py` 的 `check_access()`）：

| 上下文 | 放行条件 |
|--------|---------|
| 群聊 | 一律放行 —— 未绑定的群在框架入口（`GroupBinding`）就被整条丢掉了，插件不必再判 |
| 群聊临时会话（小窗） | 来源群 ∈ `binding.groups`（留空则取框架 `bound_groups`）**或** 用户在 `binding.users` |
| 直接私聊 | 用户 ∈ `binding.users` |
| 超级用户 | **始终放行**（防止白名单配错后谁都改不回来 —— 改白名单的命令本身也在私聊里跑）|

要点：

1. **推送目标 ≠ 私聊白名单**。`users`（推送）只是「发通知给谁」，`binding.users` 才是
   「谁能私聊调命令」。`/buptnews add user 10001` 不会顺带开放私聊服务，反之亦然。
2. **默认最小权限**：`binding` 两个清单都留空时，非超级用户的私聊一律不服务（含 `get`）。
   群聊不受影响 —— 本次改动**只**收紧私聊。
3. **`binding.groups` 留空 = 沿用框架 `bound_groups`**（`/groups` 看到的那份）。
   注意框架的 `bound_groups` 为空 = 监听**所有**群，语义与白名单相反，
   所以那份只在非空时才拿来兜底；想收紧小窗范围就显式写 `binding.groups`。
4. **`/help` 与执行同口径**：范围内看不到本命名空间不是 bug —— 插件用框架的
   `add_visibility_filter` 装了同一条判定，用不了的地址连功能清单也看不到
   （与「无权限 = 未知命令」的隐藏口径一致）。
5. **被拒时静默**（不回复、不提示「你不在白名单」）：与权限不足同为「不暴露命令存在」。
   排查时看日志 `私聊命令被服务范围拒绝：用户 …`（同一用户 60s 内只记一条，带累计次数）。

配置示例（`config.json`）：

```json
"plugins": {
  "bupt_news": {
    "binding": {
      "groups": [1000000001, 100000003, 100000002],
      "users":  [10000001, 100000004]
    }
  }
}
```

等价于：这三个群的成员在**群小窗**里可用命令；这两个 QQ 可直接私聊用；其余私聊不服务。

### 三个容易踩的点

1. **群聊里必须 @ 机器人**才会触发命令（框架 `command_dispatcher` 的行为）；私聊直接发即可。
2. **被拒绝时回的是「未知命令」**，这是刻意设计（`("_not_found", cmd)` 哨兵，
   用于隐藏命令存在性），不是 bug。权限不足与命令不存在**措辞相同**。
3. 子命令的 `permission`/`scope` **现在由框架真正校验**（见「已知限制」中的修复记录）；
   带子命令的父命令是「命令组」，本体不设权限，特权逻辑都落在子命令上。

### 权限实际效果（已实测）

角色层级：`superuser(100) > owner(80) > admin(60) > member(20) > everyone(0)`；
私聊上下文里非超级用户一律按 `member(20)` 处理。

| 场景 | superuser + 仅私聊<br>(status / targets / add / del / on / off / interval / images / bind) | admin + 私聊/群聊<br>(sync) | 无权限<br>(list：私聊/群聊；get：仅私聊) | 命令组本体<br>(`/buptnews`) |
|------|:---:|:---:|:---:|:---:|
| 群内 超级用户 | ⛔ 仅私聊 | ✅ | ✅ list（get 仅私聊）| ✅（只打印帮助）|
| 群内 群主 / 管理员 | ⛔ 仅私聊 | ✅（角色缓存命中时）| ✅ list | ✅ |
| 群内 普通成员 | ⛔ 未知命令 | ⛔ 未知命令 | ✅ list | ✅（只打印帮助）|
| 私聊 超级用户 | ✅ | ✅ | ✅ | ✅ |
| 私聊 范围内用户（小窗 / `binding`）| ⛔ 未知命令 | ⛔ 未知命令 | ✅ | ✅（只打印帮助）|
| 私聊 范围外用户 | ⛔ 静默无响应 | ⛔ 静默无响应 | ⛔ 静默无响应 | ⛔ 静默无响应 |

> ⚠️ **最后一行是本次新增的闸门**：范围外的私聊连「未知命令」都不回（静默丢弃），
> `/help` 里也看不到本命名空间。判定与配置见 §四之六。

> ⚠️ **群主/管理员能否过 `admin` 门槛依赖角色缓存**：框架只在收到 `GroupAdminEvent`
> （设置/取消管理员）时写入群角色，**启动时不会拉取群成员列表**。冷启动后群主会被
> 当成 `member`。最稳妥的做法：把自己的 QQ 加进 `config.json` 的 `superusers`
> （当前为 `10000001`）。

示例（超级用户 QQ 10000001，目标群 1000000001）：

```
(私聊) /buptnews sync preview          # 先确认能抓到、能收到
(私聊) /buptnews add group 1000000001
(私聊) /buptnews add user 10001
(私聊) /buptnews status
```

### 消息排版约定（所有面向用户的消息）

为了让群里/私聊里的消息一眼能扫，本项目统一按下面的样式排版（改消息时请沿用）：

```
📢 一级标题                 ← 首行：图标 + 标题，不带句号
                            ← 空行分段
⚙️ 分区名                    ← 分区标题：图标 + 名字，顶格
  字段：值                   ← 字段行：两个空格缩进，"键：值"（全角冒号）
  状态：✅ 开 / ⏸ 关          ← 状态值用 ✅/⏸/⚠️，不用"是/否"

📊 另一个分区
  字段：值

💡 /buptnews xxx — 用法      ← 结尾给一条最该用的命令；多条用 " · " 连接
```

- 列表条目：一行一条，**行首是编号**（如 `12 摘要`），导航行用 `⬅️ 上一页：… · ➡️ 下一页：…`；
- 末页显式写 `✅ 已是最后一页`，空态写 `（未配置）` + 提示；
- `/help` 列表分两段：`[📦 命令组]`（命令组）与 `[📖 命令]`（无子命令的命令），
  每行 `  /名字 — 介绍`；**第一级不按权限分段**（用不了的命令根本不列）；
  **权限与作用域写在后两（三）种入口**：命令组页把
  `[权限：xxx · 作用域：…]` 挂在每个子命令名后，单命令/子命令详情单独一行
  （公开且不限场景也写全 `权限：公开 · 作用域：所有场景`）；
- `/buptnews get` 头部是 `📢 #编号 标题` + `🏛 部门 · 日期` + 分隔线，正文随后，结尾 `🔗 原文（含附件）：…`；
- 早报（digest）**没有条目时只发天气行**（不加标题/提示），有条目时：天气行 → 空行 → `📋 今日通知（N 条）` → 条目 → 空行 → `💡` 提示。

---

## 四点五、作用域（命令能在哪里用 / 影响什么）

「作用域」在本项目里有三层，含义完全不同：

### 1. 命令的 `scope` 参数 —— 子命令各不相同

| `scope` | 命令 |
|---------|------|
| `private`（仅私聊） | `status` `get` `targets` `add` `del` `on` `off` `interval` `images` `bind` |
| `all`（私聊 + 群聊） | 父命令 `buptnews`（命令组本体）、`list` `sync` |

> `scope` 只管「在私聊还是群里」，**不回答「这个私聊能不能用」** —— 后者是 §四之六 的
> `binding`（服务范围），判定发生在权限校验**之前**。

框架在 `CommandManager.handle()` 里**按实际执行的那条命令（子命令优先）**判定：

```python
if not effective.allows_context(is_group):      return ("_not_found", cmd)

# CommandHandler.allows_context
if self.scope == "private" and is_group:     return False
if self.scope == "group"   and not is_group: return False
```

> 历史坑已修：`scope`/`permission` 原先只校验**父命令**，子命令的声明完全不生效。
> 现在 `handle()` 先解析「实际执行的 handler」（子命令优先）再校验，`/help` 也用同一条
> `allows_context()` 判定，两者不会再分叉。
> 例外：**带子命令的命令是命令组，本体不校验权限**（权限属于子命令），但本体的 `scope`
> 仍然生效。

### 2. ⚠️ 真正决定「机器人听哪个群」的是 `bound_groups`

真正卡住群聊的是**框架入口的事件过滤**，比 `scope` 严格得多。
`napcat/client.py` 的 `_on_ws_event()`：

```python
if (event.message_type == "group"
        and event.group_id is not None
        and not self.groups.is_bound(event.group_id)):
    return                       # 事件在入口被丢弃，根本进不了 EventBus
```

当前 `config.json` 是 `bound_groups: [1000000001]`、`all_groups: false`，实测：

| 群号 | `is_bound` |
|------|------------|
| `1000000001` | ✅ True |
| 其它任意群 | ❌ False |

也就是说：**只有 1000000001 这个群的任何事件会被处理**，其它群的消息、入群、
禁言、戳一戳等全部在入口丢弃。**私聊永远不受影响**（代码只对 `group` 判定）。

> 🪤 **陷阱**：`config.json` 里的 `all_groups` 在 `bound_groups` 非空时会被**忽略**：
> ```python
> all_groups=all_groups if bound_groups is None else not bool(bound_groups)
> ```
> 所以想「监听所有群」必须把 `bound_groups` **清空**（`[]`），
> 单把 `all_groups` 改成 `true` 是没用的。

### 3. 命令影响的「数据作用域」是全局的

`groups` / `users` / `interval` / `enabled` / `columns` 都存在
`plugins.bupt_news.*` 下，是**全局配置，不区分群**：

- `/buptnews add group X` 改的是 Bot 的全局广播列表，在哪个群执行都一样；
- 定时同步没有会话上下文，会把新通知发给**所有**已配置目标。

框架本身支持按群隔离配置（`self.config.group(gid)`，`essential` 插件的欢迎语就是这么做的），
但本插件的推送目标用的是全局配置。若希望「每个群各自订阅、互不影响」，
需改用 `self.config.group(gid)` 存目标，并给命令加上 `scope="group"`。

---

## 五、工作流程

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
  每次同步后按「仍被保留记录引用」清理缓存。**推送/早报不带图片**（只有摘要行）。
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
  这是刻意换取的防风控余量；压测时可把 `api_delay` 设为 `0`。

---

## 六、自检

```bash
# 离线：解析 / 状态 / ID / 免打扰 / 早报 / 摘要 / 命令分发（用夹具与桩件，不联网）
venv-arm/bin/python -u tests/test_bupt_news.py

# 在线：真实门户 + 真实 Open-Meteo + 真实 DeepSeek + 全链路早报
BUPT_PORTAL_USERNAME=... BUPT_PORTAL_PASSWORD=... DEEPSEEK_API_KEY=... \
  venv-arm/bin/python -u tests/test_bupt_news.py --online

# 只诊断门户链路（带每步耗时）
BUPT_PORTAL_USERNAME=... BUPT_PORTAL_PASSWORD=... \
  venv-arm/bin/python -u tests/diagnostics/probe_portal.py
```

当前状态：**离线 410+ 项断言**（`--online` 追加在线链路检查，需门户凭据）。
其中 4 项与代码现状不符、为**既有失败**（`A17`/`A3`/`A22` 摘要推送格式仍按带 `#` 前缀的
旧格式断言，而推送早改成 `ID 摘要`），与本插件的入群问候撤回改动无关。

---

### 7.4.1 正文表格 → 图片

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

---

## 七、已知限制

- **✅ 已收紧：私聊命令只服务「绑定群的小窗 + 指定用户」**（此前任何私聊用户都能跑
  `list` / `get`，包括非好友的陌生人）。现在多了一道**独立于权限等级**的闸门：
  只有**绑定群的群聊临时会话（小窗）**与 `binding.users` 里的用户能私聊用命令，
  两者都空时非超级用户一律不服务（**默认最小权限**）；群聊不受影响，超级用户豁免
  （否则白名单配错就没人能改回来）。判定与配置见 §四之六，管理命令
  `/buptnews bind user|group <号> [add|del]`（superuser）。
  - 为什么**不做成权限节点**（`perm.register(...)` 挂到命令上）：权限节点只有两态，
    会把 `list`/`get` 这类**公开**命令一并抬成白名单专属，且 `/perm set` 能把它改成
    别的值而让收紧悄悄失效 —— 服务范围是「这个地址能不能用」，与「这个人有没有资格」
    是两件事，必须分层。
  - 强制点在**命令登记处**统一包一层守卫（`_register_commands()` 里替换
    `self.register_command`），后加的子命令不会漏；`/help` 用框架的
    `add_visibility_filter` 复用**同一条**判定，所以范围内外「看得到 = 用得了」。
  - ⚠️ 写这类守卫时踩到框架的两个签名陷阱（已写进 `_guarded` 注释，自检
    `[5.7]` 用 `inspect.signature` 兜住）：`functools.wraps` 会把 `__signature__`
    指回原函数，而框架是**按参数名**注入的 —— 守卫不显式暴露 `packet`（事件唯一
    可靠来源）与 `params`，就会「非白名单用户照样能 `list`」或「原函数收不到参数」
    这类**静默**故障。
  - 被拒时静默（与「无权限 = 未知命令」一致，不暴露命令存在），只按用户 60s 节流记日志。
  - 自检脚本 `[5]`/`[5.5]`/`[5.7]` 按代码断言（准入矩阵、`/help` 同口径、
    白名单增删、默认值、写坏的配置），全部通过。
- **✅ 已修复：框架不校验子命令权限**（原先 `@register_command("a.b", permission=...)`
  里的 `permission` 完全不生效，父命令的 `admin` 会掩盖子命令的 `superuser`）。
  修复位置：`napcat/command.py` 的 `CommandManager.handle()` 现在会先解析
  「实际执行的 handler」（子命令优先）再校验其 scope/permission。
  顺带修了同源的第二个缺陷：`register()` 的 `permission`/`scope` 默认值由 `""`/`"all"`
  改为 `None`，使文档承诺的**子命令继承父命令**真正生效（显式传值仍可覆盖）。
  插件里此前的 `require()` 绕行已随修复删除（避免重复 owner）。
- **✅ 调整：三种帮助入口都写明「权限 + 作用域」**（框架级，`napcat/command.py`）：
  - `/help buptnews`（命令组页）→ 每个子命令行挂 `[权限：xxx · 作用域：…]`，
    本体另写「本体无需权限 · 作用域：…」（本体不校验权限，但 `scope` 对它生效）；
  - `/help buptnews.list`（子命令详情）/ `/help <叶子命令>` → 单独一行
    `权限：公开 · 作用域：所有场景` —— **公开且不限场景也写全**，不留空白；
  - 两处用同一句话（`describe_requirements()`；清单里的方括号标注由 `requirement_tag()`
    复用同一函数），不会出现「详情说只看私聊、清单不写」这种口径分叉；
  - 自检 `[5.5]` 与出图用例按新格式重写（`[权限：superuser · 作用域：仅私聊]` 等）。
- **✅ 调整：裸调命令组 / 用法不正确 → 一律重定向到对应帮助页**（框架级，`napcat/command.py`）：
  - `/buptnews`（裸调命令组）→ 直接回 `/help buptnews` 那一页（本体描述 + 有权使用的子命令）；
    **注册在父命令上的函数不再运行** —— 本插件与 `essential` 里手写的「打印本体用法」
    函数因此成为死代码（保留仅为满足注册签名）；
  - `/buptnews nosuch`（子命令名写错）→ 同样回命令组帮助页（正好列出可用的子命令名）；
  - 参数写错（Schema 校验失败）→ `⚠️ 用法不正确：<原因>` + **该（子）命令**的帮助页
    （用法行、参数表、默认值、`权限：…`）；
  - 随之退役 `on_schema_error` 钩子与两个插件的回调：那个钩子只有一个槽位，两个插件都
    注册时后者覆盖前者，用户看到哪种报错取决于插件加载顺序；
  - 顺带修了 Schema 报错同一字段重复两遍的缺陷（位置位循环与「其余字段」循环各记一次）。
  - 自检 `[5]`/`[5.5]`/`[8.8]` 按新口径重写：回执看 `handle()` 的返回值，路由仍看
    `_reply_command`（小窗带来源群）。
- **✅ 调整：`/help` 第一级只列「命令组」与「没有子命令的命令」**。此前第一级把每个子命令
  也按自身权限分组列出（如 `[🔓 所有人可用]` 下的 `/buptnews list`），几十条子命令把命令组
  本身淹掉，同一份子命令清单还在第一级与 `/help <命令组>` 两处渲染。现在：
  - `/help` = 命令组（`[📦 命令组]`）+ 无子命令的命令（`[📖 命令]`），**两段同版式**，
    每行只给「命令名 — 介绍」；**不按权限分段**（`[🔓 所有人可用]` / `[🛡️ admin]` /
    `[👑 superuser]` 这些标题已取消 —— 用不了的命令本来就不列，再分段只会把同一类命令拆开）；
    计数 N = 可见的命令组数 + 无子命令命令数；
  - `/help <命令组>` = 该组的子命令清单（**只有名称 + 描述**，不带参数/用法/权限标注；
    按调用者过滤，口径同执行），参数与权限在下一级 `/help <命令组> <子命令>`；
  - `/help <主命令> <子命令>` = 该子命令的介绍 + 用法（与 `/help <主命令>.<子命令>` 等价）；
  - 列表结尾提示改为 `💡 /help <命令名> 查看子命令或用法`；
  - 自检 `[5.5]` 按新口径重写：第一级断言「不含任何子命令行」，子命令的权限/范围可见性
    断言移到 `/help buptnews` 的第二级视图上。
- **✅ 已修复：`/help` 权限指示挂在父命令上**（与上一条同源：子命令的权限能被校验了，
  但 `/help` 仍把子命令一律塞进父命令的 `permission` 分组、且只按父命令的 `scope` 过滤。
  于是群聊里会看到「`[🛡️ admin]` 分组下的 `/buptnews add`」—— 实际它需要 `superuser` 且仅私聊，
  在群里执行只会得到「未知命令」）。
  修复位置：`napcat/command.py` 的 `_cmd_help()` 现在按**每条命令自身**的 `permission`
  分组、按自身 `scope` 过滤（**后续调整**：子命令不再进第一级列表，第二级的子命令行
  也只剩「名字 + 描述」；权限/范围改在第三级详情行呈现，`requirement_tag()` 随之删除）；
  `handle()` 与 `/help` 共用新增的 `CommandHandler.allows_context()`，避免两处口径再次分叉；
  自定义权限节点（如 `permission="vip"`）的命令也不再被 `/help` 整条吞掉。
- **✅ 已修复：命令组（有子命令的命令）本体被当成有权限**。原先父命令的
  `permission="admin"` 既是子命令默认值、又被拿来拦截 `/buptnews` 本体，`/help` 也把
  `/buptnews` 排进 `[🛡️ admin]` 分组 —— 而本体只是打印用法。
  现在统一为：**父命令的 `permission`/`scope` 只作为子命令默认值，本体不设权限**。
  - `handle()`：执行到命令组本体时跳过权限校验（`scope` 仍生效，如 `group_config` 仅群聊）；
  - `/help`：命令组单列 `[📦 命令组]` 分组，`/help buptnews` 的本体说明写明
    「命令组（本体无需权限，权限见各子命令）」；
  - `/commands`（superuser）：命令组本体不报权限，并逐条列出各子命令自己的 `scope`/`权限`。
- **✅ 已修复：文档/自检与代码不一致**（4 项既有失败：`status`、`add`、`interval` 被当成
  `admin`+群内可用）。现按代码为准：
  `status`/`targets`/`add`/`del`/`on`/`off`/`interval` = **superuser + 仅私聊**，
  `sync` = **admin + 私聊/群聊**，`list` = **无权限 + 私聊/群聊**，
  `get` = **无权限 + 仅私聊**。
  §四命令表、§四点五作用域与权限矩阵已重写，自检脚本 `[5]`/`[5.5]` 按代码断言
  （自检脚本按代码断言，全部通过）。
- **✅ 已调整：`list` / `get` 放开为无权限**（群成员可自行查编号、私聊可看全文，
  不再需要 admin）。实现方式：注册时显式写 `permission=""` 覆盖父命令的 `admin` 默认值
  （`register()` 用 `None`/`""` 区分「未声明」与「显式无权限」），`sync` 仍为 `admin`。
  `/help` 中二者归入 `[🔓 所有人可用]` 分组，详情行显示 `[公开]` / `[公开 · 仅私聊]`
  （后续调整：这些权限分段与标签都已取消 —— 子命令不进第一级列表，第二级只给「名字 + 描述」，
  权限/范围统一在 `/help buptnews <子命令>` 的 `权限：…` 行里）；
  自检脚本 `[5]`/`[5.5]` 增加了普通成员在群里跑 `list`、普通用户私聊跑 `get` 的断言。
- **✅ 优化：掉线期间的 API 调用保留到服务器重新上线**。此前 `WsHandler.send()` 在未连接时
  直接抛 `ConnectionError`，于是 NapCat 一重启/掉线，插件这一轮的推送、`get`、命令回复全部当场失败。
  现在 `send()` 先 `_wait_ready()`：**没连接就排队等着**（日志提示「API 调用开始排队」），
  重连成功后按序补发并打印「开始补发 N 个排队中的 API 调用」；`offline_hold_timeout`
  （默认 0 = 一直等）与 `offline_queue_max`（默认 1000）分别控制等待上限与堆积上限，
  进程退出时排队调用被取消而不是挂死。**已经发出但没等到响应的调用仍立即失败**（不自动重发，
  避免消息重复）。自检脚本 `[10]` 新增 8 项断言（排队/补发/上限/超时/退出）。
- **✅ 优化：`/help` 只展示对方权限可访问的命令**。此前 `/help` 把所有命令连权限档一起列出
  （普通成员也能看到 `superuser` 专属命令），与执行时「无权限 = 未知命令」的隐藏口径相反。
  现在列表按 `scope` + `check_permission` 双重过滤（分组标题仍是该命令**所需**权限），
  `/help <无权限命令>` 返回「未知命令」、`/help <命令组>` 只列可用子命令
  （后续调整：第一级只列命令组与无子命令命令、**且不再按权限分段**（见上一条），首行计数改为
  可见的命令组数 + 无子命令命令数；子命令的过滤在 `/help <命令组>` 这一级生效），
  命令组本体不设权限的规则在过滤里同样成立（否则父命令上的
  `permission="admin"` 会把整个命令组对普通成员隐藏）。自检脚本 `[5.5]` 新增 10 项断言。
- **✅ 已修：群聊临时会话（小窗）收不到回复**。小窗消息在 OneBot 里是
  `message_type="private"` + `sub_type="group"` + 来源 `group_id`，而框架的
  `send_private_msg` 没有 `group_id` 参数、`_reply_command` 对 private 一律按好友私聊发送 ——
  NapCat 于是把它当好友私聊处理，**非好友（小窗的典型情形）直接发送失败**，表现为「小窗里发命令没反应」。
  现在：`ApiClient.send_private_msg(..., group_id=…)` 支持临时会话，
  `_reply_command` 通过新增的 `MessageEvent.is_temp_session` 自动带回来源群（分段回复每一段都带）；
  `bupt_news` 的 `preview` 与 `essential` 的参数错误提示也按来源回（小窗回小窗，
  不会把「参数错误」泄露到群里）。（后续调整：用法错误的回执改由框架统一产出，
  路由仍走 `_reply_command`；见下方「帮助重定向」调整。）另外修了自检脚本的时间敏感问题：默认免打扰窗口在
  23:00–07:00 会抑制推送，导致「白天全绿、半夜全红」，现在自检开始时显式关闭免打扰。
  自检脚本 `[8.8]` 有 10 项断言守护（真实 NapCat 载荷 → 解析 → 命令 → 实际 API 参数）。
- **✅ 新增：群聊 `/help` 显式提示「需先 @机器人」**。群聊里没 @bot 的命令会在框架入口被丢弃
  （连 `/help` 自己也是），但帮助输出过去只列命令，用户容易以为「复制命令就能用」。
  现在群聊下的 `/help`（首行下方）与 `/help <命令名>`（末尾）都会带一行
  `⚠️ 群里需先 @机器人再发命令（直接复制命令不触发）`；私聊不显示，
  仅私聊的 `scope="private"` 命令也不会误报（它只显示「仅私聊」）。
  提示语作为 `napcat.GROUP_AT_HINT` 导出，插件自带帮助（如 `/buptnews`）复用同一句。
  自检脚本 `[5.5]` 有 5 项断言守护（群里有/私聊没有/仅私聊命令不误报/插件帮助同样提示）。
- **✅ 已修：字符串参数不支持单引号转义**。命令解析用 POSIX `shlex`，而它**在单引号内不处理
  反斜杠**：`/echo 'don\'t'` 会被判成「引号没配对」→ 走宽松回退 → 引号被当成字面量留下
  （`'don't'`），多词文本还会被拆散。现在分词前把 `\'` 换成内部哨兵、分词后再还原，
  于是单引号内也能转义单引号；`\\'`（转义反斜杠 + 引号）语义不变。
  自检脚本 `[2.5]` 有 12 项解析断言守护，并在 `[5.6]` 里用真实命令（`/group_config greeting
  'We\'re BUPT!'`）端到端验证。
- **✅ 已修：入群问候一直崩（`essential` 插件）**。`greeting` 处理器读的是**全局**
  `plugins.essential.greeting`，而开关与设置命令都写在**按群**的
  `groups.<群号>.greeting` 里 —— 群里明明配了欢迎语，处理器却取到 `None`，
  于是每次有人入群都抛 `TypeError: can only concatenate str (not "NoneType") to str`
  （事件总线把异常吞掉只打堆栈，所以表现为「欢迎语从来不发」）。
  现在：欢迎语按**群作用域**读取（与 `greeting_enabled` 同源），空的欢迎语只记 warning
  并跳过、不再抛异常；历史上若有人用过全局 `greeting`，仍作为兜底并在日志里提示迁移。
  顺带修了设置命令的坑：`/group_config greeting`（不带内容）以前会用 schema 默认值
  `欢迎!` **覆盖**掉已有长欢迎语，现在改为保留原文、只切开关；没有欢迎语时开启会明确
  提示先设置内容。自检脚本 `[5.6]` 有 14 项断言守护（含真实群配置的回归）。

- **✅ 新增：入群欢迎语支持撤回**。`/group_config greeting` 增加可选参数 `recall_delay`
  （发出后多少**秒**撤回）；撤回只有一个旋钮，且是**三态**：

  ```
  /group_config greeting <欢迎语> [enabled] [recall_delay]
  /group_config greeting 欢迎新同学 true 60      # 60 秒后撤回
  /group_config greeting --recall_delay=2m       # 时长支持 s/m/h（2m = 120 秒）
  /group_config greeting 欢迎新同学 true         # 不带延迟 = 保持现值（不清零）
  /group_config greeting --recall_delay=0        # 显式 0 = 取消撤回
  ```

  ⚠️ **「不带」与「显式 0」必须区分开**：schema 默认值若写成 `0`，那么在 `config.json` 里
  手写好 `greeting_recall_delay`、之后又跑一次 `/group_config greeting <内容> true` 时，
  命令会用默认值把延迟重置掉、回执变成「撤回: 关」（实际踩过）。现在 `recall_delay`
  默认 `None`：省略 = 保持现值，`0` = 取消，正数 = 启用；帮助里这类字段标注
  「不带则保持原值」（`napcat/command.py` 的 `describe_fields` 统一处理）。

  口径与欢迎语本身完全一致：按群存 `groups.<群号>.greeting_recall_delay`，群内没配才回落到
  同名全局键；**默认不撤回**，所以老配置升级后行为不变，回执会回显 `撤回: 60 秒` 或
  `撤回: 关`。撤回是延迟任务（`asyncio.Task`），失败只记日志不影响问候；同一群再来新人
  会取消上一条待撤回任务（否则到点会撤回新发的那条欢迎语），插件禁用/热重载时统一取消。
  早期版本里的布尔开关 `greeting_recall` 已废弃：读到 `false`（原意「永不撤回」）会一次性
  折算为 `delay=0` 并删除该键，行为与升级前一致。
  自检脚本 `[5.6]` 新增 35 项断言守护（0 不撤回 / 正数到点撤回 / 还没到点不撤 / 从正数改回 0 /
  不带延迟保持现值 / 同群连发只留最新 / 禁用清理 / 全局兜底 / 老配置迁移 / 命令读写与非法值拒绝）。

- **✅ 已修：带位置位的参数被 `--键=值` 静默忽略**（`napcat/command.py`）。Schema 的键值
  写法（帮助里的「键值传参」行）过去只对**没有位置位**的字段生效：有位置位的字段先被
  位置参数填满，`--键=值` 那条分支就被跳过。于是 `/xxx 参数 --可选=值` 里的 `--可选=值`
  既不改值也不报错（`/group_config greeting --recall_delay=2m` 就是典型受害写法）。
  现在**显式键值优先于位置位**，值照样过字段校验（非法值报「⚠️ 用法不正确」+ 该命令帮助页，
  见下方「帮助重定向」调整）。

- **✅ 新增：下载失败重试机制 + `/buptnews images retry`**。原先图片下载失败只是记一条 warning，
  要等下一轮同步（默认 10 分钟）才会再撞一次，失败原因与次数也不留痕。现在：
  - **立即重试**：每张图失败后再试 `images.retry` 次（默认 2），间隔 `retry_delay × 2^k`
    （默认 2s、4s 指数递增）—— 门户偶发 DNS/连接抖动当场就能补回来；
  - **失败账本** `data/image_failures.json`：记录每个 URL 的累计失败次数、最后一次错误与时间，
    下载成功即移除；累计达到 `images.max_attempts`（默认 5）后自动补齐不再空撞，
    日志与 `/buptnews images` 都能看到「下载失败待重试 N 张（最近：…）」；
  - **手动重试命令** `/buptnews images retry [数量]`：忽略次数上限，把失败过的图再试一遍
    （`force` 语义），返回成功/仍失败张数。
- **✅ 已修：正文截断会把图片一起丢掉**（`body_max_chars` 默认 `800`）。图片的位置是相对
  正文的字符偏移，截断点之后的图会被判定为「不在消息里」而整批不发送 —— 实测一条 1224 字、
  3 张图的存量通知，3 张图全在 1082 字之后，默认配置下 `get` 一张都发不出来（补了等于白补）。
  现在两条都做了：默认改为 **`0`（不截断）**，且**有图片的记录恒不截断**（即使配置/内存里
  还是 `800`，`get` 也会跳过截断并记一条日志）—— 这样运行中的 bot 只要热重载插件就恢复出图，
  不必等重启；纯文字记录仍按 `body_max_chars` 截断。另外 `get` 遇到「没有 `images` 字段」的
  历史记录时会在消息里提示「尚未补图」并给出 `/buptnews images` 入口。
- **✅ 新增：存量通知补图（`backfill.py` + `/buptnews images` + `backfill_images.py`）**。
  图片功能上线前入库的记录没有 `images` 字段，`get` 只能发文字。现在同步时会重抓这类记录的
  正文页（`images.recrawl`/`recrawl_max`，默认每轮 50 条），补出图片位置并刷新正文
  （偏移与新正文必须一致），随后由补齐流程下载；重抓与下载都可续跑、单条失败不影响其它记录。
  命令 `/buptnews images [stats|backfill|download] [数量]` 可随时查看/手动补齐，
  停 bot 后也可用 `backfill_images.py` 一次性补完（默认备份 `notices.jsonl`）。
  ⚠️ 别在 bot 运行时跑脚本：bot 内存里的 store 会在下次保存时覆盖文件。
- **✅ 新增：同步时补齐图片缓存**。原先图片只在 `/buptnews get` 时懒加载，第一次查长图文
  要等所有图片下载完。现在每轮同步（**即使本轮没有新通知也跑**）都会补齐缓存：
  本轮新通知优先，随后按记录从新到旧补**所有保留记录**里「被引用但本地没有」的图片；
  每轮上限 `images.prefetch_max`（默认 30，0 = 不限），没补完的下一轮继续，
  日志给出「本轮尝试 N 张，仍有 M 张待补」。`get` 直接读本地文件；
  下载失败只记日志、不影响同步（那张图在 `get` 时还会再试一次）；
  关掉 `images.prefetch` 则完全退回懒加载。自检脚本 `[8.5]` 覆盖：新通知预下载落盘、
  无新通知时补齐历史记录、已缓存不重复下载、单轮上限与新记录优先、关闭预下载、
  失败不影响落库。
- **✅ 新增：长消息自动分段发送**。QQ 单条消息放不下很长的图文，`MessageChain.split()`
  （`napcat/message.py`）按「文字字符数 / 媒体段数」切分：文字优先在换行处断开、图片与文件段
  整段搬走、顺序不变；命令返回 `list[MessageChain]` 时客户端按序逐条发送（断线则停止后续分段）。
  插件侧配额取自 `plugins.bupt_news.chunk`（默认 1200 字 / 4 张图，0 = 不限），
  `/buptnews get` 与推送/早报共用。自检脚本 `[8.6]` 有 21 项断言守护。
- **✅ 新增：`/buptnews get` 正文图片按原排版插入**。原先 `_ContentExtractor` 把 `<img>`
  当空元素直接丢掉，正文只剩文字。现在抽取时给图片埋 sentinel、规范化后再切开，
  得到 `images: [{"pos": 字符偏移, "url": 绝对地址}]`（`parse_detail(html, base_url)`，
  兼容 `src` / `data-src` / `data-original` / `srcset` 等写法），随通知一起落库。
  `get` 用 `MessageChain` 交替输出「文字段 + 图片段」，图文位置与网页一致。
  图片经 `PortalClient.fetch_bytes()`（同一个已登录会话 + CAS 重登）下载到 `data/images/`，
  按 URL 去重、按文件头判类型，**NapCat 侧不需要能访问门户、也不需要携带 Cookie**；
  单张超过 `images.max_bytes`（默认 10MB）跳过，下载失败只丢那一张并在消息末尾说明张数。
  `body_max_chars` 截断时，落在被截断部分之后的图片不再发送。
  新增 `plugins/bupt_news/images.py`（缓存 / 判型 / 清理），自检脚本 `[8.5]` 有 40 项断言守护。
- **✅ 已修复：WebSocket 任何报错都不重连**（原先 `WsHandler` 存了
  `reconnect_interval` 却**没有任何重连代码**：连接一断，接收循环退出，主循环继续空转，
  机器人从此收不到任何消息；NapCat 未启动时 `start()` 直接抛异常退出）。
  现在由 `WsHandler.run_forever()` 托管连接：首次连接失败、被对端关闭、收发抛任何异常
  都会自动重连（`reconnect_interval` 起、2 倍指数退避、封顶 60s），连上后重新拉
  `get_login_info` 刷新 `self_id`；断线时挂起的 API 请求立即以 `ConnectionError` 失败，
  不再白等 30s 超时；`stop()` 先停重连循环再关连接。自检脚本 `[10]` 用本地回环
  WebSocket 服务覆盖「服务端断开重连 / 首次连接失败后重试 / 挂起请求立即失败」。
- **✅ 新增：所有 API 上报带 0.5–1.0s 节流**。`ApiClient` 的每个调用发送前等待
  `api_delay`（默认 `[0.5, 1.0]` 随机值）并**串行化**，降低被 QQ/NapCat 风控的概率。
  配置项：`config.json` 的 `api_delay`（数字 = 固定值，`[min,max]` = 区间，`0` = 关闭），
  也可用 `NapCatClient(api_delay=...)` 覆盖。自检脚本 `[10]` 断言默认区间、
  单次等待与并发串行化。
- **✅ 已修复：包目录插件热重载失效**。`napcat/plugin.py` 新增 `_plugin_files()`，
  `watch()` 现在监控包内全部 `.py`；`reload()` 会清理整棵
  `napcat_plugin_<name>.*` 模块子树（此前子模块残留在 `sys.modules`，重载后仍跑旧代码）。
- **⚠️ DeepSeek 摘要是 best-effort**：偶发失败时该批降级为**标题截断**（不会丢通知）。
  实测在连续多轮调用中出现过整批返回空的情况。当前**没有重试**（按规格 §7 的降级设计），
  失败会以 `[bupt_news] 摘要失败（N 条将降级）` 记入日志。若觉得降级太频繁，
  可考虑给摘要加 1 次重试（与门户登录的 3 次重试保持一致）。
- **✅ 已修复：测试会污染真实数据**。`on_load()` 原先用 `__file__` 硬编码数据目录，
  测试无法拦截，定时同步一旦触发就会写真实 `plugins/bupt_news/data/`
  （实测把 `state.json` 覆盖成与 `notices.jsonl` 不一致的状态）。
  现在数据目录由 **`data_dir` 配置**接管，测试在 `load_all()` 之前把它指向临时目录，
  并有断言守护。
- **⚠️ 导入历史前请先停掉 bot**：`import_history.py` 会写 `state.json` 与
  `notices.jsonl`；若 bot 同时在跑，它内存里的旧状态会在下次 `save()` 时覆盖
  `state.json`，造成两者不一致。`StateStore` 已加**自愈**——加载时会把
  `idCounter` 抬到既有记录的最大 ID（只增不减），避免之后 `add_notice()` 分配出
  与既有记录冲突的 ID；但仍建议停 bot 再导入。
- **依赖 NapCatQQ**：本插件只负责「抓取 + 调用发送 API」，真正的 QQ 登录与发送由
  NapCatQQ（OneBot11）完成。
- **门户为校内资源**：`my.bupt.edu.cn` 需校园网或 atrust/VPN；Bot 所在主机必须能访问它。
- 本机 `systemd-resolved` 偶发解析失败（`Name or service not known`），插件已内置重试，
  表现为「本轮跳过，下轮恢复」。

---

## 七点五、排障：WebSocket 连不上时看异常类型

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

### 正确配置

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

### 三步自查

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

### 摘要服务（DeepSeek）的三个坑

> 端点由 `summarize.base_url` / `model` / `api_key` 决定，**环境变量只覆盖 key**。
> 校内网关与官方 API 都兼容 OpenAI `/chat/completions`，换端点只改这三个值即可。
> `import_history.py --summarize` 与插件读的是**同一份** `config.json`，不会各走各的。

**0. 校内网关有 30 次/窗口 的请求限额，且只放行白名单模型**

实测（`https://myai.bupt.edu.cn/llm-gw/v1`）：

| 现象 | 说明 |
|---|---|
| 超额返回 **HTTP 429** | `Rate limit exceeded for user: cas_xxx. Current limit: 30`，响应里带 `Limit resets at` |
| 换模型名返回 **403** | `key not allowed to access model. This key can only access models=['deepseek-v4-flash']` |
| 上游故障时返回 **HTTP 500** | `rrgw.InternalServerError: upstream - Internal Server Error` |

⚠️ **415 条 ÷ `batch_size=10` = 42 次调用 > 30**，一次跑必然超额。
给校内网关做批量摘要时，把 `batch_size` 调到 **≥ 20**（415 条 → 21 次），
或分多次运行（工具只处理「尚无摘要」的记录，可以安全续跑）。

**1. `max_tokens` 给小了会「静默返回空摘要」**

`deepseek-flash` 是**推理模型**，reasoning 占约 93% 的 completion token。实测：

| `max_tokens` | 结果 |
|---|---|
| 64 | ❌ `finish_reason=length`，`content` 为**空串** |
| 1024 | ✅ 单条可用（约耗 291 token，其中 reasoning 282）|
| 3072 | ✅ 6 条批量可用（约耗 1551 token，其中 reasoning 1436）|

所以 `summarize.max_tokens` 默认给到 4096，且 `_one_batch` 遇到空 `content` 会**显式抛错**
而不是返回空摘要——否则会静默推送空消息。

**2. 思考链可以关掉，而且应该关掉（`reasoning_effort`）**

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

### 7.3 AI 摘要重试

摘要失败（限流 429、上游 5xx、超时、空 `content`、AI 少返回某几条）以前会被**静默降级**
成标题截断并写进库里，之后**再也没有机会**补上 —— 因为库里已经有值了。

现在的链路是三层重试 + 失败可识别：

| 层 | 触发 | 重试次数 | 说明 |
|----|------|----------|------|
| ① 同步内连续重试 | 每轮同步给待推送记录摘要 | `retry`（默认 **5**） | 指数退避，间隔 `retry_delay`（默认 2s，逐次翻倍）|
| ② 下次同步自动补试 | 每轮同步末尾 | 同 ① | 扫「还没有 AI 摘要」的历史记录，单轮最多 `retry_scan_max`（默认 60）条 |
| ③ 调取时重试 | `/buptnews get <编号>` | `retry_on_notice`（默认 **2**） | 交互路径要快，所以次数比同步少；成功后写回库，`/buptnews list` 立刻能看到 |

补试（②）的三个工程约束，都踩过坑：

- **只能有一个调用点，且必须在所有提前 return 之前。** 放在 `return` 之后 →
  「上次 AI 失败、之后再没新通知」的记录永远等不到重试；调用两次 → 同一轮同步跑两遍补试，
  API 调用翻倍（曾同时放在早返回前和推送前，实测一轮触发两次）。
- **节流**：上一轮一条都没补成功时冷却 `retry_cooldown`（默认 1800s），否则每轮同步
  （间隔 10 分钟）都会对同一条失败记录重复烧 API。只要还有进展就继续下一批，不等待。
  注意冷却期内**不要**因为"待补恰好为空"而重置冷却（那会让冷却提前失效）。
- **补试不赶时间**：单请求超时用 `retry_timeout`（默认 120s，比 `summarize.timeout` 宽松）、
  批量用 `retry_batch_size`（默认 6，比 `summarize.batch_size` 小）——
  实测网关较慢时 10 条/批会 60s 超时，整批白丢。

`_judge_only` 还有一个坑：**没有进展就立刻停**。曾经写成"整批失败就重试 `attempts` 次"，
于是一次补试对同一批重复请求 6 遍（60 条 × 6 = 360 次调用，且每次都整批超时）。

**时间预算（早报延迟的教训）**

早报与补试曾经排在同一个"先做 AI、再推送"的串行流程里，于是：

- 补试要处理几十条历史记录 → 早报被挡在后面；
- 本轮待推送记录的摘要失败会**指数退避重试 6 次**（2+4+8+16+…）→
  实测一轮同步多花 **60s+**；
- 网关域名解析失败时，退避等待纯属白等。

现在三层都受**时间预算**约束，到点就带着已有结果先发，剩下的留给下一轮：

| 阶段 | 预算配置 | 默认 | 说明 |
|------|----------|------|------|
| 本轮推送的摘要 | `summarize.push_budget` | 25s | 时间敏感：到点就用已有摘要渲染，缺的降级为标题截断 |
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

```
[bupt_news] AI 摘要失败（同步，3/10 条缺失，第 1/6 次）：HTTP 429: rate limited；2.0s 后重试
[bupt_news] AI 摘要重试 6 次仍失败（同步，10 条）：HTTP 429: rate limited；本轮不写降级摘要，下次同步/调取时再试
```

---

### 7.4 学生通知判定（不 @全体成员）

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
> 会把整条丢掉，表现为"0 条成功、且没有任何报错"。现在 `_one_batch` 接受 `parse` 参数，
> 并且**有内容却解析不出条目时会显式报错**（把响应的 `finish_reason` 与前 120 字打进日志），
> 不再静默失败。

> 两侧提示词都强调「拿不准给 true」，所以判定偏保守：**只有模型明确说不是学生**才会
> 抑制 @全体成员。

---

## 八、与 Node 爬虫的关系

另有一套**独立的离线批量爬取**工具（`crawl.js` / `login.js`，不在本仓库内，
导出 JSONL + 附件）。本插件是**在线增量推送**，两者互相独立：

- 批量归档、要附件 → 用 `crawl.js` + `download-attachments.js`
- 实时通知、要推送 → 用本插件

两者共用同一套站点知识：CAS 登录流程、`list.jsp` 的 `totalpage` 陷阱、
`.v_news_content` 正文容器、纯图片通知正文为空等。


---

## @全体成员

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
所以每个群都用**独立的分段列表**（`chunks_for_group = list(chunks)`），加前缀只作用在
该群那份上。早期版本把加过前缀的链写回共享的 `chunks[0]`，导致第二个群收到
`@全体成员 @全体成员 正文`（同一份分段被反复加前缀）。

配置：

```json
"plugins": { "bupt_news": { "at_all": true } }
```

> 各家 OneBot 实现的字段名不完全一致，`_parse_at_all_remain()` 按
> `can_at_all` / `remain_at_all_count_for_group` / `remain_at_all_count_for_uin`
> 等已知字段解析，结果封装成 `_AtAllQuota`（`can_at_all` / `group_remain` /
> `uin_remain` / `exhausted` / `summary`）；完全认不出来时 `can_at_all=None`，
> 即「不知道」，既不 @all 也不提示。
