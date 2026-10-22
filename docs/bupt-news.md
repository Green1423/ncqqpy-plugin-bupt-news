# bupt_news 插件 —— 校内通知同步 + 免打扰早报 + ID 检索 + DeepSeek 摘要

每 10 分钟同步一次北邮信息服务门户的「校内通知」，把**新增**通知推送到指定的 QQ 群 / QQ 用户。

- **免打扰**：默认 23:00–07:00 不推送，次日首次越过窗口时随**当天天气**汇总补发
- **短 ID**：每条通知分配 `#1` 递增编号，可用 `/buptnews get <编号>` 查回全文（**图文按原排版**）与原文链接（含附件入口）
- **20 字摘要**：推送只含 DeepSeek 生成的 ≤20 汉字梗概（批量调用；失败降级为标题截断）

设计规格见 [`specs/2026-09-24-bupt-news-digest-design.md`](specs/2026-09-24-bupt-news-digest-design.md)，实施计划见 [`plans/2026-09-24-bupt-news-digest.md`](plans/2026-09-24-bupt-news-digest.md)。

相关文档：[`usage.md`](usage.md)（框架使用）· [`plugin-dev.md`](plugin-dev.md)（插件开发）· [`ai-chat.md`](ai-chat.md)（同款「服务范围」闸门）· [`byrdocs.md`](byrdocs.md)（白名单）· [`ncqq-watchdog.md`](ncqq-watchdog.md)（服务器看门狗）。

---

## 目录

1. [一、目录结构](#一目录结构)
    - [1.1 插件目录](#11-插件目录)
    - [1.2 配套脚本与入口](#12-配套脚本与入口)
2. [二、运行前提](#二运行前提)
3. [三、配置](#三配置)
    - [3.1 配置项](#31-配置项)
    - [3.2 凭据优先级](#32-凭据优先级)
4. [四、命令](#四命令)
    - [4.1 命令一览](#41-命令一览)
    - [4.2 私聊可用范围（binding）—— 两道闸门，别混为一谈](#42-私聊可用范围binding-两道闸门别混为一谈)
    - [4.3 权限实际效果（已实测）](#43-权限实际效果已实测)
    - [4.4 三个容易踩的点](#44-三个容易踩的点)
    - [4.5 消息排版约定（所有面向用户的消息）](#45-消息排版约定所有面向用户的消息)
5. [五、作用域（命令能在哪里用 / 影响什么）](#五作用域命令能在哪里用--影响什么)
    - [5.1 命令的 scope 参数 —— 子命令各不相同](#51-命令的-scope-参数--子命令各不相同)
    - [5.2 真正决定「机器人听哪个群」的是 bound_groups](#52-真正决定机器人听哪个群的是-bound_groups)
    - [5.3 命令影响的「数据作用域」是全局的](#53-命令影响的数据作用域是全局的)
6. [六、工作流程](#六工作流程)
    - [6.1 同步与推送流程](#61-同步与推送流程)
    - [6.2 正文表格渲染为图片](#62-正文表格渲染为图片)
    - [6.3 @全体成员](#63-全体成员)
    - [6.4 学生通知判定（不 @全体成员）](#64-学生通知判定不-全体成员)
7. [七、已知限制](#七已知限制)
8. [八、排障](#八排障)
    - [8.1 WebSocket 连不上：先看异常类型](#81-websocket-连不上先看异常类型)
    - [8.2 正确配置](#82-正确配置)
    - [8.3 三步自查](#83-三步自查)
    - [8.4 摘要服务（DeepSeek）的三个坑](#84-摘要服务deepseek的三个坑)
    - [8.5 AI 摘要重试与时间预算](#85-ai-摘要重试与时间预算)
9. [九、与 Node 爬虫的关系](#九与-node-爬虫的关系)
10. [十、测试与自检](#十测试与自检)

---

## 一、目录结构

### 1.1 插件目录

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

### 1.2 配套脚本与入口

配套文件（不在插件目录内）——自检脚本、诊断探针与一次性维护脚本都在 **`tests/`** 下，仓库根只留运行入口（`main.py` / `run.sh` / `config.json` / `napcat/` / `plugins/`）：

| 文件 | 说明 |
|------|------|
| [tests/test_bupt_news.py](../tests/test_bupt_news.py) | 自检脚本（离线 **490+ 项**：解析 / 状态 / ID / 免打扰 / 早报 / 摘要 / 命令 / 权限与 `/help` / **私聊可用范围** / 图片排版 / 长消息分段 / WS 重连与上报节流；`--online` 追加在线联调）|
| [tests/diagnostics/probe_portal.py](../tests/diagnostics/probe_portal.py) | 在线诊断：分步验证 登录 → 列表 → 正文，带每步耗时 |
| [tests/diagnostics/probe_summarize.py](../tests/diagnostics/probe_summarize.py) | 摘要接口调用形状探针（逐条 vs 批量、token 预算）|
| [tests/backfill_images.py](../tests/backfill_images.py) | 一次性补齐存量通知的正文图片（重抓 + 下载，自动备份 notices.jsonl）|
| [run.sh](../run.sh) | 启动脚本（自动用 `venv-arm`，并从 `.env` 读取密码）|
| `.env.example` | 环境变量模板（仓库暂未提供，可自行创建：门户密码 + DeepSeek key）|

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

3. **门户账号**（统一身份认证）——见 [§3.2 凭据优先级](#32-凭据优先级)。

---

## 三、配置

### 3.1 配置项

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
| `summarize` | `{enabled,base_url,model,api_key,batch_size,max_tokens,timeout,reasoning_effort,retry,retry_delay,retry_on_notice,retry_scan_max,retry_cooldown,retry_timeout,retry_batch_size,push_budget,retry_budget}` | DeepSeek 摘要；`batch_size` 默认 3，`max_tokens` 默认 4096；失败重试见 [§8.5 AI 摘要重试](#85-ai-摘要重试与时间预算) |
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
| `binding` | `{groups:[], users:[]}` | **私聊可用范围**：谁能私聊（含群小窗）用命令。`groups` = 允许在**群聊临时会话（小窗）**里用命令的来源群，留空则沿用框架的 `bound_groups`（谁被监听就允许谁的小窗）；`users` = 允许**直接私聊**用命令的 QQ 号。两者都空 = 非超级用户私聊一律不服务（含 `get`）。群聊内不受影响；超级用户豁免。用 `/buptnews bind user\|group <号>` 增删，详见 [§4.2](#42-私聊可用范围binding-两道闸门别混为一谈) |
| `username` | — | 门户账号（学号/工号）|
| `password` | `""` | 门户密码（建议改用环境变量）|
| ~~`push_body`~~ | — | **已退役**：推送不再携带正文，正文恒被抓取落库以供 `get` 查询。键名保留但被忽略 |

### 3.2 凭据优先级

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

### 4.1 命令一览

命令前缀取自 `config.json` 的 `command_prefixes`（默认 `/` 和 `#`）。
下表按 `plugins/bupt_news/main.py` 的**实际注册声明**列出（`scope`/`permission` 以代码为准）：

| 命令 | 范围 | 权限 | 说明 |
|------|------|------|------|
| `/buptnews` | 私聊 / 群聊 | 无（命令组本体） | 帮助：打印全部子命令用法 |
| `/buptnews status` | 仅私聊 | **superuser** | 状态：开关、间隔、免打扰窗口、目标数、上次同步、上次早报、累计推送、可查询条数 |
| `/buptnews list [页码]` | 私聊 / 群聊 | 无 | 分页列出通知编号（最新在前，每页 `list_page_size` 条；标注 `第 x/y 页，共 N 条`）|
| `/buptnews get <编号\|E编号\|P编号\|链接>` | 仅私聊 | 无 | 查看通知全文：标题/部门/日期 + **正文图文按原页面排版** + 原文链接；**过长自动拆成多条消息**。也接受**原文链接**（门户检索结果没有本地编号，直接贴链接即可看全文）|
| `/buptnews search <关键词> [每页] [页码] [日期]` | 仅私聊 | 无 | **搜索本地库 + 信息门户**。本地：标题/摘要/正文/单位/编号（子串、不区分大小写）；门户：标题或全文（`--content=false` 只搜标题，走门户自己的检索）。`--source=local\|portal` 可只搜一侧 |
| `/buptnews pin [编号\|E编号 ...] [force]` | 仅私聊 | **superuser** | 置顶通知（钉在 `list` 顶部，**每页都显示**）；可多个（`pin 12 34` / `pin 12,E1759-142679`）；不带参数=查看当前置顶。每条置顶分配**独立编号 P1/P2…**。**上限 `pin_limit`（默认 3）**：满了会拒绝并提示；加 `--force`（或 `-f`）才替换**最早置顶**的那条。**持久化在 `state.json` 的 `pins`** |
| `/buptnews unpin [P编号\|编号\|E编号 ...]` | 仅私聊 | **superuser** | 取消置顶（`unpin P2` 摘掉第 2 个置顶项）；不带参数=清空全部 |
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

### 4.2 私聊可用范围（`binding`）—— 两道闸门，别混为一谈

私聊（`message_type="private"`，**含群聊临时会话即小窗**）的准入与「命令权限」是**两套独立**的判定，
先过范围、再过权限（小窗在 OneBot 里的形态与回复路由见 [usage.md §6.2.2](usage.md#622-群聊临时会话小窗)）：

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
   [`add_visibility_filter`](plugin-dev.md#48-给整组命令加一道自己的闸门服务范围--白名单) 装了同一条判定（同类闸门的另一份实现在 [ai-chat.md §二](ai-chat.md#二服务范围只服务绑定的群--私聊)），用不了的地址连功能清单也看不到
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

### 4.3 权限实际效果（已实测）

角色层级（见 [usage.md §7.1](usage.md#71-角色层级)）：`superuser(100) > owner(80) > admin(60) > member(20) > everyone(0)`；
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
> `/help` 里也看不到本命名空间。判定与配置见 [§4.2](#42-私聊可用范围binding-两道闸门别混为一谈)。

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

### 4.4 三个容易踩的点

1. **群聊里必须 @ 机器人**才会触发命令（框架 `command_dispatcher` 的行为，见 [usage.md §6.2.1](usage.md#621-群聊里必须-机器人)）；私聊直接发即可。群聊下的 `/help` 首行也会提示这一点。
2. **被拒绝时回的是「未知命令」**，这是刻意设计（`("_not_found", cmd)` 哨兵，
   用于隐藏命令存在性），不是 bug。权限不足与命令不存在**措辞相同**。
3. 子命令的 `permission`/`scope` **由框架按子命令自己的声明校验**（见 [usage.md §6.5](usage.md#65-子命令)）；
   带子命令的父命令是「命令组」，本体不设权限，特权逻辑都落在子命令上。

### 4.5 消息排版约定（所有面向用户的消息）

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
  （公开且不限场景也写全 `权限：公开 · 作用域：所有场景`）；三级入口的构成见 [usage.md §6.1.1](usage.md#611-help-三级入口帮助文档的构成)；
- 参数写错（Schema 校验失败）回 `⚠️ 用法不正确：<原因>` + **该（子）命令**的帮助页；
  裸调命令组、子命令名写错也都重定向到对应的帮助页（见 [usage.md §6.1.2](usage.md#612-帮助重定向裸调命令组--用法不正确)）；
- `/buptnews get` 头部是 `📢 #编号 标题` + `🏛 部门 · 日期` + 分隔线，正文随后，结尾 `🔗 原文（含附件）：…`；
  过长按 `chunk` 拆成多条消息时，**原文链接整段留在一条里**（不会出现「上一条结尾 `…/notice/`、下一条开头 `2026/…`」点不开的情况）—— 由框架的`MessageChain.split()` 保证，见 `tests/test_message.py`；
- 早报（digest）**没有条目时只发天气行**（不加标题/提示），有条目时：天气行 → 空行 → `📋 今日通知（N 条）` → 条目 → 空行 → `💡` 提示。

---

## 五、作用域（命令能在哪里用 / 影响什么）

「作用域」在本项目里有三层，含义完全不同：

### 5.1 命令的 `scope` 参数 —— 子命令各不相同

| `scope` | 命令 |
|---------|------|
| `private`（仅私聊） | `status` `get` `targets` `add` `del` `on` `off` `interval` `images` `bind` |
| `all`（私聊 + 群聊） | 父命令 `buptnews`（命令组本体）、`list` `sync` |

> `scope` 只管「在私聊还是群里」，**不回答「这个私聊能不能用」** —— 后者是 [§4.2](#42-私聊可用范围binding-两道闸门别混为一谈) 的
> `binding`（服务范围），判定发生在权限校验**之前**。

框架在 `CommandManager.handle()` 里**按实际执行的那条命令（子命令优先）**判定：

```python
if not effective.allows_context(is_group):      return ("_not_found", cmd)

# CommandHandler.allows_context
if self.scope == "private" and is_group:     return False
if self.scope == "group"   and not is_group: return False
```

> `handle()` 先解析「实际执行的 handler」（子命令优先）再按它自己的 `scope`/`permission`
> 校验，`/help` 用同一条 `allows_context()` 判定，两者不会分叉（见 [usage.md §6.5](usage.md#65-子命令)）。
> 例外：**带子命令的命令组本体不校验权限**（权限属于子命令），但本体的 `scope` 仍然生效。

### 5.2 真正决定「机器人听哪个群」的是 `bound_groups`

⚠️ 真正卡住群聊的是**框架入口的事件过滤**，比 `scope` 严格得多。
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

### 5.3 命令影响的「数据作用域」是全局的

`groups` / `users` / `interval` / `enabled` / `columns` 都存在
`plugins.bupt_news.*` 下，是**全局配置，不区分群**：

- `/buptnews add group X` 改的是 Bot 的全局广播列表，在哪个群执行都一样；
- 定时同步没有会话上下文，会把新通知发给**所有**已配置目标。

框架本身支持按群隔离配置（`self.config.group(gid)`，`essential` 插件的欢迎语就是这么做的），
但本插件的推送目标用的是全局配置。若希望「每个群各自订阅、互不影响」，
需改用 `self.config.group(gid)` 存目标，并给命令加上 `scope="group"`。

---

## 六、工作流程

### 6.1 同步与推送流程

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
  这是刻意换取的防风控余量（见 [usage.md §2.4](usage.md#24-断线重连与上报节流)）；压测时可把 `api_delay` 设为 `0`。
- **掉线期间的调用会排队**：NapCat 未上线时发送调用排队等待（`offline_hold_timeout` 默认 `0`
  = 一直等，`offline_queue_max` 默认 1000），重连后按序补发；**已发出但没等到响应**的调用
  仍立即失败、不自动重发，避免消息重复（见 [usage.md §2.4](usage.md#24-断线重连与上报节流)）。
- **小窗命令按来源回复**：群聊临时会话（小窗）的回复会自动带回来源群
  （`MessageEvent.is_temp_session`），不会发成好友私聊；`preview` 与用法错误提示同样按来源回
  （见 [usage.md §6.2.2](usage.md#622-群聊临时会话小窗)）。

### 6.2 正文表格渲染为图片

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

### 6.3 @全体成员

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

### 6.4 学生通知判定（不 @全体成员）

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

### 7.6 置顶与搜索

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

#### 三种编号

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

#### 门户检索（本地库里没有的旧通知也能搜到）

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

## 七、已知限制

- **⚠️ DeepSeek 摘要是 best-effort**：偶发失败时该批降级为**标题截断**（不会丢通知），
  失败会以 `[bupt_news] 摘要失败（N 条将降级）` 记入日志；实测在连续多轮调用中出现过整批
  返回空的情况。重试用尽时本轮**不写降级摘要**，留给下次同步或调取时再试（见
  [§8.5 AI 摘要重试](#85-ai-摘要重试与时间预算)）。
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

## 八、排障

### 8.1 WebSocket 连不上：先看异常类型

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

### 8.2 正确配置

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

### 8.3 三步自查

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

### 8.4 摘要服务（DeepSeek）的三个坑

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

### 8.5 AI 摘要重试与时间预算

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

```
[bupt_news] AI 摘要失败（同步，3/10 条缺失，第 1/6 次）：HTTP 429: rate limited；2.0s 后重试
[bupt_news] AI 摘要重试 6 次仍失败（同步，10 条）：HTTP 429: rate limited；本轮不写降级摘要，下次同步/调取时再试
```

---

## 九、与 Node 爬虫的关系

另有一套**独立的离线批量爬取**工具（`crawl.js` / `login.js`，不在本仓库内，
导出 JSONL + 附件）。本插件是**在线增量推送**，两者互相独立：

- 批量归档、要附件 → 用 `crawl.js` + `download-attachments.js`
- 实时通知、要推送 → 用本插件

两者共用同一套站点知识：CAS 登录流程、`list.jsp` 的 `totalpage` 陷阱、
`.v_news_content` 正文容器、纯图片通知正文为空等。

---

## 十、测试与自检

自检脚本与诊断探针都在 `tests/` 下（见 [§1.2 配套脚本与入口](#12-配套脚本与入口)）：

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
