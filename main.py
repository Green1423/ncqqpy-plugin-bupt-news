"""校内通知同步 + QQ 推送插件。

功能
----
* 按固定间隔（默认 600s = 10 分钟）轮询 my.bupt.edu.cn 的「校内通知」栏目
* 只推送**新增**通知（持久化去重）
* **免打扰**：默认 23:00–07:00 不推送，次日首次越过窗口时随当天天气汇总补发
* **短 ID**：每条通知分配 `#1` 递增编号，可 `get` 回全文与原文链接（含附件入口）
* **正文图片**：抓正文时一并抽出图片及其在正文中的位置，`get` 时按原排版把图片插回
  文字之间（图片带门户 Cookie 下载到本地缓存后再发送，NapCat 无需能访问门户）
* **图片缓存补齐**：每轮同步结束时补齐缓存 —— 本轮新通知优先，随后是**所有保留记录**里
  仍缺的图（每轮上限 `images.prefetch_max`，没补完下一轮继续），`get` 直接读本地文件；
  没下到的图在 `get` 时还会再试
* **历史记录补图**：加图片功能之前入库的记录没有图片信息，同步时会重抓其正文页补出图片
  位置（每轮 `images.recrawl_max` 条），也可用 `/buptnews images backfill [数量]` 一次多补些；
  一次性补齐用 `backfill_images.py`（需先停 bot）
* **长消息分段**：`get` / 推送 / 早报超过 `chunk` 配额时自动拆成多条消息按序发送
* **DeepSeek 摘要**：推送只含 ≤20 汉字梗概（批量调用；失败降级为标题截断）
* 可推送到任意 QQ 群 / QQ 用户，支持命令动态增删（"指定"）
* 首次运行只建立基线不推送，避免刷屏
* 支持 `preview` 模式：只私聊发给自己，用于验证链路

配置（config.json → plugins.bupt_news）
----------------------------------------
    interval            同步间隔秒数，默认 600
    enabled             推送总开关，默认 true（关闭后仍同步记录，不发送）
    columns             栏目 wbtreeid 列表，默认 [1154]（校内通知）
    groups              推送目标群号列表
    users               推送目标 QQ 列表
    quiet_hours         {start:"23:00", end:"07:00"} 免打扰窗口（支持跨午夜）
    weather             {enabled,label,latitude,longitude,timeout,umbrella_pop}
    summarize           {enabled,base_url,model,api_key,batch_size,max_tokens,timeout,
                         reasoning_effort,retry,retry_delay,retry_on_notice,
                         retry_scan_max}
                        AI 摘要失败自动重试：同步时连续重试 retry 次（默认 5，
                        指数退避 retry_delay），重试用尽**不写降级摘要**，
                        下次同步或用户 /buptnews get 调取该条时再试（retry_on_notice）。
                        摘要同时会判定「是否发给学生」：判定为否 → 消息加
                        「疑似不是给学生的通知」提示，且**不 @全体成员**
    data_dir            状态与记录目录（默认插件目录下 data/；测试/多实例须覆盖）
    digest_max_items    早报最多列出多少条，默认 40
    list_page_size      /buptnews list 每页条数，默认 20
    notice_retention    正文保留条数（供 get 查询），默认 500
    body_max_chars      纯文字通知的正文截断长度，默认 0 = 不截断；**有图片的通知恒不截断**
                        （截断点之后的图片会被丢掉），长文一律交给 chunk 分段发送
    tables              {enabled, max_per_notice} 正文表格 → 图片（Chromium 渲染，
                        按表格内容哈希缓存在 data/tables/；正文文本里表格会被拍平，
                        只有渲染成图才能保住行列关系）
    images              {enabled, max_bytes, prefetch, prefetch_max, recrawl, recrawl_max,
                         retry, retry_delay, max_attempts}
                        正文图片：下载缓存并随 get 按原排版插入，默认
                        {enabled:true, max_bytes:10485760, prefetch:true, prefetch_max:30,
                         recrawl:true, recrawl_max:50}。
                        prefetch=同步时补齐缓存（本轮新通知 + 所有保留记录缺失的图），
                        prefetch_max=单轮下载上限；recrawl=给无 images 字段的历史记录重抓正文页
                        补图，recrawl_max=单轮重抓上限（都支持 0=不限）；max_bytes=单张上限 10MB；
                        retry=失败后立刻重试次数（默认 2），retry_delay=重试起始间隔秒（默认 2，
                        指数递增），max_attempts=累计失败多少次后停止自动重试（默认 5，命令可强制重试）
    chunk               {max_chars, max_images} 长消息分段配额，默认 {1200, 4}；
                        0 表示该维度不限（文字优先在换行处断开，图片/媒体段不拆）
    binding             {groups, users} 私聊（含群小窗）可用范围白名单：
                        groups=允许在**群聊临时会话（小窗）**里用命令的来源群，
                        留空则沿用框架的 `bound_groups`（谁被监听就允许谁的小窗）；
                        users=允许直接私聊用命令的 QQ 号。
                        两者都空 = 非超级用户私聊一律不服务（含 get）。
                        群聊内不受影响；超级用户豁免（防止把自己锁在外面）。
                        用 `/buptnews bind user|group <号>` 增删（见下）
    max_push_per_round  单轮最多推送条数，默认 8
    first_run_backfill  首次运行回填条数，默认 0（只建基线不推送）
    username            门户账号（学号/工号）
    password            门户密码（也可用环境变量，见下）
    push_body           **已退役**（正文恒抓取并落库；该键保留但被忽略）

凭据优先级（门户与摘要一致）：环境变量 > config.json。
    BUPT_PORTAL_PASSWORD=xxx DEEPSEEK_API_KEY=sk-xxx venv-arm/bin/python main.py

命令（前缀取自 config.json，默认 `/` 和 `#`）
-----------------------------------------------
用法里的占位符统一写作 `<名称>`（必填）与 `[名称]`（可选），尖括号/方括号本身不用输入。

    /buptnews                 帮助
    /buptnews status          查看状态
    /buptnews list [页码]     分页列出通知编号（最新在前）
    /buptnews get <编号>      查看该通知全文（图文按原排版；过长自动分多条）与原文链接
    /buptnews targets         查看推送目标
    /buptnews sync [页数] [preview]      立即同步
    /buptnews add [group|user] [号]      添加目标（省略群号则用当前群）
    /buptnews del [group|user] [号]      移除目标
    /buptnews on | off        开关推送
    /buptnews interval [时长] 设置间隔，如 10m / 600
    /buptnews search <关键词> [每页] [页码] [日期]   本地库 + 信息门户（门户结果给 E 编号）
    /buptnews pin|unpin [编号|E编号|P编号 ...]      置顶（独立编号 P1/P2…，存 state.json）
    /buptnews images [stats|backfill|download|retry] [数量]
                              图片缓存：统计 / 补齐历史图片 / 只下图片 / 重试失败的下载
    /buptnews bind [user|group] <号> [list|add|del]
                              私聊可用范围白名单：谁能私聊用本插件
                              （省略号数 = 只看当前白名单）

权限：查询/同步需 admin（私聊中非超级用户不满足，等同仅超级用户）；
      增删目标与开关需 superuser。

私聊可用范围（`binding`）
------------------------
私聊（含群小窗）里只有这些调用者能用命令：

* **超级用户** —— 始终可用（防止白名单把自己锁在外面，也是唯一能改白名单的人）；
* 来源群在 `binding.groups`（留空则取框架 `bound_groups`）里的**群聊临时会话**；
* QQ 号在 `binding.users` 里的用户（**直接私聊**）。

群聊内不受此限制（群消息本来就已被框架的 `bound_groups` 过滤）；判定与
`/help` 的可见性同口径 —— 用不了的地址在帮助里也看不到本命名空间。
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import os
import re
import time
import traceback
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

from napcat.command import GROUP_AT_HINT
from napcat.message import MessageChain
from napcat.plugin import PluginBase

# @全体成员 次数用尽时追加的醒目提示（提醒同学注意查收，而不是漏看）
_AT_ALL_EXHAUSTED_HINT = (
    "⚠️ 本条为重要通知 ⚠️\n"
    "本群 @全体成员 次数已用尽，无法提醒所有人。\n"
    "请留意本群消息，避免错过通知 🙏"
)

# AI 判定「不是发给学生的」时加在消息前面的提示，并且**不再 @全体成员**
# （既然不是给学生的，@全体成员只会打扰全群）
_NOT_STUDENT_HINT = "⚠️ 疑似不是给学生的通知"


class _AtAllQuota:
    """`get_group_at_all_remain` 的查询结果。

    推送前必须先查这个接口，拿到「能不能 @全体成员」和「还剩几次」再决定：

    - ``can_at_all=True``  → 第一条消息挂 @全体成员；
    - ``can_at_all=False`` → 次数已用尽，补一条醒目提示提醒同学查收；
    - ``can_at_all=None``  → 接口失败 / 字段不认 / 未开启，**既不 @all 也不提示**
      （误判成「用尽」会导致每轮都刷一条提示）。

    ``group_remain`` / ``uin_remain`` 是接口给出的剩余次数（拿不到时为 None），
    只用于日志展示，判定统一看 ``can_at_all`` —— 两者不一致时以服务端为准。
    """

    __slots__ = ("can_at_all", "group_remain", "uin_remain")

    def __init__(self, can_at_all: bool | None,
                 group_remain: int | None = None,
                 uin_remain: int | None = None):
        self.can_at_all = can_at_all
        self.group_remain = group_remain
        self.uin_remain = uin_remain

    @property
    def exhausted(self) -> bool:
        """服务端明确答复「不能 @全体成员」。"""
        return self.can_at_all is False

    def __bool__(self):
        return self.can_at_all is True

    def __repr__(self):  # pragma: no cover - 仅调试用
        return (f"_AtAllQuota(can_at_all={self.can_at_all!r}, "
                f"group_remain={self.group_remain!r}, "
                f"uin_remain={self.uin_remain!r})")

    @property
    def summary(self) -> str:
        """给日志用的一行描述：是否可用 + 剩余次数。"""
        if self.can_at_all is None:
            return "未知（接口未返回可用字段）"
        state = "可用" if self.can_at_all else "不可用"
        counts = []
        if self.group_remain is not None:
            counts.append(f"本群剩余 {self.group_remain} 次")
        if self.uin_remain is not None:
            counts.append(f"账号剩余 {self.uin_remain} 次")
        if not counts:
            counts.append("剩余次数未知")
        return f"{state}（{'，'.join(counts)}）"


def _as_int(value) -> int | None:
    """尽量把接口返回的值转成 int，转不了返回 None。"""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_at_all_remain(data) -> _AtAllQuota:
    """解析 `get_group_at_all_remain` 的返回。

    NapCat 的字段是 ``can_at_all`` / ``remain_at_all_count_for_group`` /
    ``remain_at_all_count_for_uin``；其它 OneBot 实现的字段名不完全一致，这里按
    已知的几种取，都认不出来就把 ``can_at_all`` 置为 None（表示「不知道」）。
    """
    if not isinstance(data, dict):
        return _AtAllQuota(None)

    group_remain = uin_remain = None
    for key in ("remain_at_all_count_for_group", "remain", "remain_count",
                "at_all_remain"):
        if key in data:
            group_remain = _as_int(data[key])
            if group_remain is not None:
                break
    for key in ("remain_at_all_count_for_uin", "remain_at_all_count_for_user"):
        if key in data:
            uin_remain = _as_int(data[key])
            if uin_remain is not None:
                break

    can_at_all: bool | None = None
    for key in ("can_at_all", "canAtAll"):
        if key in data:
            can_at_all = bool(data[key])
            break
    else:
        # 没有明确的可用字段时，退回用剩余次数推断
        if group_remain is not None:
            can_at_all = group_remain > 0
        elif uin_remain is not None:
            can_at_all = uin_remain > 0

    return _AtAllQuota(can_at_all, group_remain, uin_remain)


from .backfill import (EMPTY_DOWNLOAD_STATS, download_missing_images,
                       missing_image_urls, recrawl_missing_images,
                       records_missing_images, refresh_one_record)
from .images import FailureLog, ImageCache
from .table_image import TableImageStore, is_renderable, parse_table
from .portal import Notice, PortalAuthError, PortalClient, PortalError
from .schemas import (
    AddTargetSchema,
    BindSchema,
    DelTargetSchema,
    GetSchema,
    ImagesSchema,
    IntervalSchema,
    ListSchema,
    PinSchema,
    SearchSchema,
    SyncSchema,
    UnpinSchema,
)
from .store import (PIN_LIMIT_REACHED, StateStore, decode_external,
                   external_code, external_url, is_external_code)
from .summarize import Summarizer, fallback_summary
from .weather import WeatherClient, weather_line

SYNC_TIMER = "bupt_news.sync"
# AI 摘要的全部默认值（单一来源）：`on_load` 逐键写入 config，
# `_summary_cfg` 也用它做兜底 —— 两处各写一份必然会漂移。
SUMMARY_DEFAULTS = {
    "enabled": True,
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key": "",
    # ⚠️ batch_size / timeout 必须与网关实际延迟匹配。实测当前网关：
    #   1 条 7.5s、3 条 39.2s（都成功）、6 条 60.6s（**超时，0/6**）。
    # 原来 batch_size=10 + timeout=60 的组合必然超时 → 摘要永远拿不到、
    # 只能降级成标题，看起来就像"AI 总结没生效"。
    "batch_size": 3,
    "max_tokens": 4096,
    "timeout": 90,
    "reasoning_effort": "none",
    # 失败重试：单轮同步内连续重试次数与间隔（指数退避）
    "retry": 5,
    "retry_delay": 2.0,
    # 用户调取单条时的重试次数（交互路径要快）
    "retry_on_notice": 2,
    # 每轮最多补试多少条历史记录（0 = 不限）
    "retry_scan_max": 60,
    # 上一轮一条都没补成功时的冷却秒数
    "retry_cooldown": 1800.0,
    # 本轮**推送**的 AI 时间预算（秒）：时间敏感，超了就先发。
    # 必须 >= 一次请求的 timeout，否则单次尝试会被硬砍在最慢的网关上
    # （曾设 25s，而 timeout 是 60s → 排在前面的尝试永远没机会成功）。
    "push_budget": 200.0,
    # 后台**补试**的总时间预算（秒）
    "retry_budget": 90.0,
    # 补试的单请求超时 / 批量（比推送路径宽松 / 更小）
    "retry_timeout": 120,
    "retry_batch_size": 6,
}

SEEN_SCOPE = "_all"          # 全局去重桶：同一通知在多个栏目出现也只算一条
DEFAULT_COLUMNS = [1154]

COLUMN_NAMES = {
    "1154": "校内通知",
    "1305": "公示公告",
    "1221": "校内新闻",
    "2001": "校内文件",
    "2002": "校内通报",
    "1536": "规章制度",
    "1524": "办事指南",
}


class BuptNewsPlugin(PluginBase):
    name = "bupt_news"
    version = "1.0.0"
    description = "每 10 分钟同步北邮校内通知并推送到指定 QQ 群/用户"
    author = "bupt-news"

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self):
        # 数据目录可通过 `data_dir` 配置覆盖。
        # 默认在插件目录下；**测试与多实例部署必须覆盖它**，否则会写到真实数据上
        # （测试套件曾因此污染真实 store：state.json 被测试进程覆盖成不一致状态）。
        configured = str(self.config.get("data_dir") or "").strip()
        self._data_dir = configured or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(self._data_dir, exist_ok=True)
        self._portal: PortalClient | None = None
        self._weather: WeatherClient | None = None
        self._summarizer: Summarizer | None = None
        self._images: ImageCache | None = None
        self._img_failures: FailureLog | None = None
        self._tables: TableImageStore | None = None
        self._syncing = False
        self._command_names: list[str] = []
        # 私聊服务范围的日志节流表与 /help 可见性过滤器（见 check_access）
        self._scope_denied: dict[int, tuple[float, int]] = {}
        self._visibility_filter = None
        # 摘要补试节流：上一轮一条都没补成功时，记下下次最早可尝试的时间
        self._summary_retry_next_at = 0.0
        # 门户检索结果短时缓存：{(keyword, content, page, size): (ts, result)}
        self._portal_search_cache: dict = {}

        # 配置项由 `config_template()` 在加载时由框架统一补进 config.json
        # （只补缺、不覆盖、幂等，见那里的说明）—— 这里不再逐条 setdefault：
        # 那种写法在嵌套段上会失效，而且默认值会散落在两个地方。
        self._store = StateStore(
            os.path.join(self._data_dir, "state.json"),
            os.path.join(self._data_dir, "notices.jsonl"),
            retention=int(self.config.get("notice_retention", 500) or 500))
        self.logger.info("[bupt_news] 已加载（数据目录 %s）", self._data_dir)
        self._warn_summary_misconfig()

    # ------------------------------------------------------------------ 配置

    def config_template(self) -> dict:
        """完整配置模板（框架在加载时写进 `config.json` → `plugins.bupt_news`）。

        三件事在这里一次说清：

        1. **每一项都在文件里可见**：运维打开 `config.json` 就能看到有哪些开关可改，
           而不是只能翻代码（模板由框架 `PluginBase._seed_config()` 在构造插件时补：
           只补缺、
           不覆盖、幂等）；
        2. **嵌套段逐键补**（`weather` / `images` / `summarize` / `binding` …）：
           老写法 `setdefault("images", {...})` 在用户已经配了 `images` 段时**整段
           不生效**，新增的子键全部退化成代码里的兜底值 —— 这个坑踩过不止一次；
        3. **凭据留空**：`password` 只写空串占位（读取时环境变量
           `BUPT_PORTAL_PASSWORD` 优先），不要把真实密码写进模板。
        """
        return {
            # 数据目录：留空 = 插件目录下的 data/（测试与多实例部署必须覆盖它）
            "data_dir": "",
            # ---- 同步与推送 ----
            "interval": 600,                      # 同步周期（秒）
            "enabled": True,                      # 推送总开关（关闭后仍同步记录，不发送）
            "columns": list(DEFAULT_COLUMNS),     # 栏目号
            "groups": [],                         # 推送目标群
            "users": [],                          # 推送目标用户（私聊）
            "max_push_per_round": 8,              # 单轮最多推送几条
            "first_run_backfill": 0,              # 首次运行回填多少条历史
            "quiet_hours": {"start": "23:00", "end": "07:00"},
            "notice_retention": 500,              # 本地保留多少条通知
            "digest_max_items": 40,               # 摘要/推送里最多带多少条
            "list_page_size": 20,                 # /buptnews list 每页条数
            "body_max_chars": 0,                  # 纯文字正文截断长度，0 = 不截断
            # ---- 天气 ----
            "weather": {"enabled": True, "label": "北京",
                        "latitude": 39.9608, "longitude": 116.3590,
                        "timeout": 20, "umbrella_pop": 50},
            # ---- AI 摘要（默认值的单一来源见 SUMMARY_DEFAULTS）----
            "summarize": dict(SUMMARY_DEFAULTS),
            # ---- 正文图片 ----
            "images": {"enabled": True, "max_bytes": 10 * 1024 * 1024,
                       "prefetch": True, "prefetch_max": 30,
                       "recrawl": True, "recrawl_max": 50,
                       "retry": 2, "retry_delay": 2.0, "max_attempts": 5},
            # ---- 正文表格 → 图片 ----
            "tables": {"enabled": True, "max_per_notice": 5},
            # ---- 长消息分段发送 ----
            "chunk": {"max_chars": 1200, "max_images": 4},
            # ---- 私聊（含群小窗）可用范围白名单 ----
            "binding": {"groups": [], "users": []},
            # ---- 门户账号（密码建议只放环境变量）----
            "username": os.environ.get("BUPT_PORTAL_USERNAME", ""),
            "password": "",
        }

    def _warn_summary_misconfig(self) -> None:
        """摘要相关配置自检 —— 把「必然超时」的组合在启动时就说出来。

        这几个值互相约束，配错了表现是"AI 总结没生效"（摘要永远拿不到、只降级成标题），
        而从日志上很难看出是参数问题：
          * `push_budget` 若小于 `timeout`，推送路径的单次尝试会被预算硬砍在最慢的
            网关上（曾 25s vs 60s）；
          * `batch_size` 太大时，网关延迟乘以条数就会超过 `timeout`（实测 6 条 60.6s
            直接超时，3 条 39.2s 才成功）。
        """
        sc = self.config.get("summarize") or {}
        if not sc.get("enabled", True):
            return
        timeout = float(sc.get("timeout", SUMMARY_DEFAULTS["timeout"]) or 0)
        budget = float(sc.get("push_budget", SUMMARY_DEFAULTS["push_budget"]) or 0)
        batch = int(sc.get("batch_size", SUMMARY_DEFAULTS["batch_size"]) or 0)
        if timeout and budget and budget < timeout:
            self.logger.warning(
                "[bupt_news] summarize.push_budget（%.0fs）小于 summarize.timeout"
                "（%.0fs）：推送时的摘要尝试会被预算提前砍断，"
                "摘要会一直降级成标题。建议 push_budget >= timeout。",
                budget, timeout)
        if batch >= 6:
            self.logger.warning(
                "[bupt_news] summarize.batch_size=%d 偏大：网关延迟随条数线性增长"
                "（实测 6 条约 61s 会超时、3 条约 39s 可用），"
                "建议降到 3 或调大 summarize.timeout。", batch)

    def _backfill_summary_flags(self) -> int:
        """给旧记录补 `summary_ai` 标记，返回补了几条。

        `summary_ai` 是后加的标记，此前入库的记录只有 `summary`。只有摘要
        **不等于**标题截断（fallback）时才可能是 AI 生成的 —— 直接补标记，
        避免把这些记录当成「缺摘要」重新问一遍 AI（几百条 = 几百次多余调用），
        也避免 /buptnews list 突然全部退回显示标题。与降级文本一致或没有摘要的
        记录不补标记，留给补试链路。
        """
        updates: dict = {}
        for rec in self._store.all_records():
            if rec.get("summary_ai"):
                continue
            s = (rec.get("summary") or "").strip()
            if not s:
                continue                        # 真没摘要 → 留给补试
            if s == fallback_summary(rec.get("title", "")).strip():
                continue                        # 与降级文本一致 → 不认，留给补试
            updates[rec["id"]] = {"summary_ai": True}
        if updates:
            self._store.update_notices(updates)
            self.logger.info("[bupt_news] 已为 %d 条历史记录补写 AI 摘要标记",
                             len(updates))
        return len(updates)

    async def on_enable(self):
        # 兼容旧数据：`summary_ai` 标记是后加的，启用时给历史记录补一次标记
        # （失败不影响启用 —— 最坏情况只是那些记录会被补试重新摘要）
        try:
            self._backfill_summary_flags()
        except Exception as e:  # noqa: BLE001
            self.logger.warning("[bupt_news] 补写摘要标记失败：%s", e)

        self._portal = PortalClient(
            logger=self.logger,
            cookie_file=os.path.join(self._data_dir, "cookies.json"),
        )
        w = self.config.get("weather") or {}
        self._weather = WeatherClient(
            label=w.get("label", "北京"), latitude=w.get("latitude", 39.9608),
            longitude=w.get("longitude", 116.3590),
            timeout=int(w.get("timeout", 20) or 20),
            umbrella_pop=int(w.get("umbrella_pop", 50) or 50),
            logger=self.logger,
        ) if w.get("enabled", True) else None

        s = self.config.get("summarize") or {}
        self._summarizer = Summarizer(
            base_url=s.get("base_url", "https://api.deepseek.com"),
            model=s.get("model", "deepseek-flash"),
            api_key=os.environ.get("DEEPSEEK_API_KEY") or s.get("api_key", ""),
            batch_size=int(s.get("batch_size", 10) or 10),
            max_tokens=int(s.get("max_tokens", 4096) or 4096),
            timeout=int(s.get("timeout", 60) or 60),
            reasoning_effort=s.get("reasoning_effort", "none"),
            logger=self.logger,
        ) if s.get("enabled", True) else None

        self._img_failures = FailureLog(
            os.path.join(self._data_dir, "image_failures.json"), logger=self.logger)

        im = self.config.get("images") or {}
        self._images = ImageCache(
            os.path.join(self._data_dir, "images"),
            portal_getter=lambda: self._portal,
            enabled=bool(im.get("enabled", True)),
            max_bytes=int(im.get("max_bytes", 10 * 1024 * 1024) or 0),
            logger=self.logger,
        )

        tb = self.config.get("tables") or {}
        self._tables = TableImageStore(
            os.path.join(self._data_dir, "tables"),
            enabled=bool(tb.get("enabled", True)),
            logger=self.logger,
        )
        if tb.get("enabled", True) and not self._tables.available:
            self.logger.warning(
                "[bupt_news] 未找到 chromium，正文表格将无法渲染成图片"
                "（表格内容仍以文本形式出现）")

        self._register_commands()
        self._schedule_sync()

        if self._summarizer is not None and not self._summarizer.enabled:
            self.logger.warning(
                "[bupt_news] 未配置 DEEPSEEK_API_KEY，摘要将降级为标题截断")
        groups = self._int_list("groups")
        users = self._int_list("users")
        if not self._credentials()[1]:
            self.logger.warning(
                "[bupt_news] 未配置门户密码：请设置 config.json 的 "
                "plugins.bupt_news.password 或环境变量 BUPT_PORTAL_PASSWORD"
            )
        if not groups and not users:
            self.logger.warning(
                "[bupt_news] 未配置推送目标：用 /buptnews add group|user 添加"
            )

    async def on_disable(self):
        await super().on_disable()
        self.cancel_schedule(SYNC_TIMER)
        for cname in list(self._command_names):
            try:
                self.unregister_command(cname)
            except Exception:
                pass
        self._command_names = []
        for client in (self._portal, self._weather, self._summarizer):
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
        if self._img_failures is not None:
            self._img_failures.save()
        self._portal = self._weather = self._summarizer = None
        self._images = None
        self._tables = None
        self._img_failures = None
        self._store.save()
        self.logger.info("[bupt_news] 已停用")

    async def on_unload(self):
        try:
            self._store.save()
        except Exception:
            pass

    # ------------------------------------------------------------------ 配置助手

    @staticmethod
    def _to_ids(value) -> set[int]:
        """配置里的「号列表」→ 正整数集合（QQ 号 / 群号）。

        `bool` 是 `int` 的子类，`True` 会被 `int()` 收成 `1` —— 配置文件里写成
        `binding: {users: true}` 这种笔误不该悄悄变成「放行 QQ 1」，直接丢弃。
        非列表的值（`users: 123` 这种少写了方括号的）按空处理，不抛异常。
        """
        if not isinstance(value, (list, tuple, set)):
            return set()
        out: set[int] = set()
        for v in value:
            if isinstance(v, bool):
                continue
            try:
                num = int(v)
            except (TypeError, ValueError):
                continue
            if num > 0:
                out.add(num)
        return out

    def _int_list(self, key: str) -> list[int]:
        return sorted(self._to_ids(self.config.get(key)))

    # ------------------------------------------------------------------ 私聊可用范围
    #
    # 私聊（`message_type="private"`，含**群聊临时会话**即小窗）与群聊的准入是
    # 两套东西：
    #   * 群聊 —— 框架 `GroupBinding` 在 `bound_groups` 之外整条丢弃，插件只管权限；
    #   * 私聊 —— 框架必须监听（好友/小窗都可能有用），由这里按白名单放行。
    # 判定见 `check_access()`；强制点只有一个：`_register_commands()` 给每条命令
    # 包的守卫（`/help` 的可见性过滤器复用同一判定，保证「看得到」= 「用得了」）。

    def _binding(self) -> dict:
        """`binding` 配置段（写坏成非字典时按空处理，不让一个笔误炸掉命令）。"""
        value = self.config.get("binding")
        return value if isinstance(value, dict) else {}

    def _bound_users(self) -> set[int]:
        """允许**直接私聊**使用命令的 QQ 号。

        ⚠️ 与推送目标 `users` 是两件事：把人加进推送目标只是给他发通知，
        不等于允许他私聊调命令；反之亦然。两个清单各自显式维护。
        """
        return self._to_ids(self._binding().get("users"))

    def _bound_groups(self) -> set[int]:
        """允许**群聊临时会话（小窗）**使用命令的来源群。

        留空则沿用框架的 `bound_groups`（`/groups` 看到的那份）—— 「机器人监听
        哪些群」与「哪些群的小窗能调命令」默认是同一批群，没必要维护两份；
        想收紧就显式写 `binding.groups`。注意框架的 `bound_groups` 为空 = 监听
        **所有**群，语义与白名单相反，所以那份只在非空时才拿来兜底。
        """
        own = self._to_ids(self._binding().get("groups"))
        if own:
            return own
        try:
            inherited = self._to_ids(self.bound_groups)
        except Exception:  # noqa: BLE001 - 拿不到框架绑定就按「没配」处理
            inherited = set()
        return inherited

    def _private_scope_hint(self) -> str:
        return ("私聊可用范围 = 绑定群的群聊临时会话（小窗）+ 指定用户，"
                "见 config.json → plugins.bupt_news.binding")

    def check_access(self, event) -> bool:
        """这条消息的调用者是否在服务范围内（**与权限等级无关**）。

        | 上下文 | 放行条件 |
        |--------|----------|
        | 群聊       | 一律放行（未绑定的群早就被框架丢掉了，插件不必再判） |
        | 群小窗     | 来源群 ∈ `binding.groups`（空则取框架 `bound_groups`）**或** 用户在 `binding.users` |
        | 直接私聊   | 用户 ∈ `binding.users` |

        **超级用户豁免**：白名单是运维决策，但改白名单的命令本身在私聊里执行 ——
        不豁免就会出现「白名单配错 → 谁（包括超管）都改不了」的死锁，
        与 ai_chat 的服务范围判定保持同一口径。
        """
        if event is None:
            return True                       # 无上下文（直接调用 / 渲染帮助）
        try:
            user_id = int(event.user_id or 0)
        except (TypeError, ValueError):
            return False
        if self.perm.is_superuser(user_id):
            return True
        if getattr(event, "message_type", "") != "private":
            return True                       # 群聊：准入由框架的 bound_groups 负责
        users = self._bound_users()
        if user_id in users:
            return True                       # 直接私聊白名单
        if getattr(event, "is_temp_session", False):
            # 用 `_to_ids` 而不是 `int(...)`：字段缺失/写成非数字时不该抛异常
            # （注意 `set() <= 白名单` 恒为真，所以不能拿空集合去比）
            source = self._to_ids([getattr(event, "group_id", None)])
            return bool(source) and source <= self._bound_groups()
        return False

    # 被服务范围挡下的调用者：`{user_id: (上次日志时间, 累计次数)}`。
    # 只用于日志节流 —— 不回复任何提示（命令存在性都不暴露，见 check_access 的调用点）。
    _SCOPE_LOG_INTERVAL = 60.0

    def _log_scope_denied(self, event) -> None:
        if event is None:
            return
        try:
            user_id = int(event.user_id or 0)
        except (TypeError, ValueError):
            return
        now = time.time()
        last, count = self._scope_denied.get(user_id, (0.0, 0))
        if now - last < self._SCOPE_LOG_INTERVAL:
            self._scope_denied[user_id] = (last, count + 1)
            return
        # 表大小封顶：长期运行下被扫到的陌生 QQ 不应无限堆积（满了就丢掉最早的一批）
        if len(self._scope_denied) >= 256:
            self._scope_denied.clear()
        source = (f"，来源群 {event.group_id}"
                  if getattr(event, "is_temp_session", False) else "")
        self._scope_denied[user_id] = (now, 0)
        self.logger.info(
            "[bupt_news] 私聊命令被服务范围拒绝：用户 %s%s（近 %.0fs 内 %d 次；%s）",
            user_id, source, self._SCOPE_LOG_INTERVAL, count, self._private_scope_hint())

    def _install_visibility_filter(self) -> None:
        """把「用不用得了」同步到 `/help` 的「看不看得到」。

        没有它，未授权的私聊发 `/help` 仍会列出本命名空间与全部子命令 ——
        功能清单与参数表等于公开，而实际执行一律静默无响应，用户只会以为机器人坏了。
        过滤器是**同步**的（框架 `is_visible()` 对同步返回值直接用），
        与守卫共用 `check_access()`，两处口径不会漂移。
        """
        cmds = self.cmds
        if not hasattr(cmds, "add_visibility_filter"):
            return                            # 老版本框架：没有钩子就不装
        if self._visibility_filter is not None:
            cmds.remove_visibility_filter(self._visibility_filter)

        def _visible(handler, event) -> bool:
            # 只看本命名空间：父节点 `/buptnews` 与其子命令；别的插件的命令不归我们管
            if getattr(handler, "parent_name", "") != "buptnews" \
                    and getattr(handler, "name", "") != "buptnews":
                return True
            return self.check_access(event)

        self._visibility_filter = _visible
        cmds.add_visibility_filter(_visible)

    def _columns(self) -> list[int]:
        out: list[int] = []
        for v in self.config.get("columns") or []:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
        return out or list(DEFAULT_COLUMNS)

    def _interval(self) -> int:
        try:
            return max(60, int(self.config.get("interval", 600) or 600))
        except (TypeError, ValueError):
            return 600

    def _credentials(self) -> tuple[str, str]:
        user = os.environ.get("BUPT_PORTAL_USERNAME") or self.config.get("username") or ""
        pwd = os.environ.get("BUPT_PORTAL_PASSWORD") or self.config.get("password") or ""
        return str(user), str(pwd)

    # ------------------------------------------------------------------ 免打扰 / 早报

    def _quiet_window(self) -> tuple[dtime, dtime]:
        q = self.config.get("quiet_hours") or {}

        def parse(v, default: dtime) -> dtime:
            try:
                h, m = str(v).split(":")
                return dtime(int(h), int(m))
            except (ValueError, AttributeError):
                return default

        return (parse(q.get("start", "23:00"), dtime(23, 0)),
                parse(q.get("end", "07:00"), dtime(7, 0)))

    def _in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        """支持跨午夜窗口（如 23:00–07:00）与同日窗口（如 01:00–06:00）。"""
        now = now or datetime.now()
        start, end = self._quiet_window()
        t = now.time()
        if start == end:
            return False
        if start < end:
            return start <= t < end
        return t >= start or t < end

    def _today(self, now: Optional[datetime] = None) -> str:
        return (now or datetime.now()).strftime("%Y-%m-%d")

    def _is_digest_due(self, now: Optional[datetime] = None) -> bool:
        """每天第一次越过免打扰窗口时为「早报」（带天气）。

        推论：机器人若在当天下午首次启动，那次同样算早报（lastDigestDate 为空
        ≠ 今天），因而带天气行——用户希望每天至少收到一次带天气的汇总。
        """
        now = now or datetime.now()
        return (not self._in_quiet_hours(now)) and \
               (self._store.last_digest_date != self._today(now))

    # ------------------------------------------------------------------ 定时

    def _schedule_sync(self) -> None:
        interval = self._interval()

        @self.schedule(interval, name=SYNC_TIMER)
        async def _sync_loop():
            await self._sync_once(reason="定时")

        self.logger.info(
            "[bupt_news] 已启用：每 %ds 同步一次；推送目标 %d 群 / %d 用户",
            interval, len(self._int_list("groups")), len(self._int_list("users")),
        )

    def _reschedule(self) -> None:
        self.cancel_schedule(SYNC_TIMER)
        self._schedule_sync()

    # ------------------------------------------------------------------ 同步主流程

    async def _sync_once(self, reason: str = "定时", pages: int = 1,
                         force_push: bool = False, preview_user: int | None = None,
                         preview_group: int | None = None):
        """执行一轮同步。

        Returns:
            (新通知数, 成功目标数, 失败目标数)，或 None 表示未完成。
        """
        if getattr(self, "_syncing", False):
            self.logger.info("[bupt_news] 上一轮同步仍在进行，跳过本次（%s）", reason)
            return None
        self._syncing = True
        try:
            return await self._do_sync(reason, pages, force_push, preview_user,
                                       preview_group)
        except PortalAuthError as e:
            self.logger.error("[bupt_news] 门户认证失败：%s", e)
            return None
        except Exception as e:
            # ⚠️ 不能传 `exc_info=True`：`self.logger` 可能是只实现
            # info/warning/error 的注入式 logger（测试替身、嵌入用法），
            # 多一个关键字参数就会**在异常处理里再抛一次**，
            # 把真正的失败原因整条盖掉。自己把堆栈格式化成文本更稳。
            self.logger.error("[bupt_news] 同步失败：%s\n%s",
                              e, traceback.format_exc())
            return None
        finally:
            self._syncing = False
            try:
                self._store.save()
            except Exception as e:
                self.logger.error("[bupt_news] 状态保存失败：%s", e)

    async def _do_sync(self, reason: str, pages: int, force_push: bool,
                       preview_user: int | None, preview_group: int | None = None):
        username, password = self._credentials()
        if not username or not password:
            self.logger.warning("[bupt_news] 未配置门户账号/密码，跳过同步")
            return None
        assert self._portal is not None
        self._portal.username = username
        self._portal.password = password

        columns = self._columns()
        col_key = ",".join(str(c) for c in columns)
        self.logger.info("[bupt_news] 开始同步（%s），栏目 %s", reason, columns)

        collected: list[Notice] = []
        for col in columns:
            try:
                items = await self._portal.fetch_list(col, pages=int(pages or 1))
                collected.extend(items)
                self.logger.info("[bupt_news] 栏目 %s 拉到 %d 条", col, len(items))
            except PortalAuthError:
                raise
            except PortalError as e:
                self.logger.error("[bupt_news] 栏目 %s 拉取失败：%s", col, e)

        if not collected:
            self.logger.warning("[bupt_news] 未获取到任何通知")
            return None

        # collected 顺序为「最新在前」
        fresh = [n for n in collected if not self._store.is_seen(SEEN_SCOPE, n.key)]
        first_run = not self._store.is_initialized(col_key)

        self._store.note_sync()
        self._store.mark_seen(SEEN_SCOPE, [n.key for n in collected])

        push: list[Notice] = []
        if preview_user is not None or force_push:
            push = list(reversed(fresh))          # 旧 → 新，阅读顺序更自然
        elif first_run:
            self._store.mark_initialized(col_key)
            backfill = max(0, int(self.config.get("first_run_backfill", 0) or 0))
            if backfill and fresh:
                push = list(reversed(fresh[:backfill]))
            self.logger.info(
                "[bupt_news] 首次同步：建立基线 %d 条，回填推送 %d 条",
                len(collected), len(push),
            )
        else:
            # 注意：这里**不能**在 fresh 为空时提前 return ——
            # 当天早报即使没有新通知也要发出（R5 / A9：只发天气行）。
            cap = max(1, int(self.config.get("max_push_per_round", 8) or 8))
            if len(fresh) > cap:
                self.logger.info(
                    "[bupt_news] 新通知 %d 条，超出单轮上限 %d，仅推送最新 %d 条",
                    len(fresh), cap, cap,
                )
                fresh = fresh[:cap]
            push = list(reversed(fresh))

        if first_run:
            self._store.mark_initialized(col_key)
        # 注意：此处**不能**因 push 为空而直接返回 ——
        # 当天早报即便没有新通知也必须发出（R5 / A9：只发天气行）。

        # ---- 落库：分配 ID 并写 notices.jsonl（供 /buptnews get 查询）----
        # push_body 已退役：正文**恒抓**，因为 /buptnews get 需要它。
        # 记录里带 `key`，用于把"已落库"与"已推送"对上账。
        if push:
            await self._fill_bodies(push)
            for n in push:
                self._store.add_notice({
                    "key": n.key, "newsid": n.newsid, "treeid": n.treeid,
                    "title": n.title, "dept": n.dept, "date": n.date,
                    "url": n.url, "body": n.body, "summary": "",
                    "images": [dict(it) for it in (n.images or [])],
                    # 表格原文（正文文本已被拍平，只有留着 HTML 才能渲染成图片）
                    "tables": [str(t) for t in (n.tables or [])],
                })
            self._prune_images()
        # 图片缓存：先给历史记录补图（重抓正文页，受 recrawl/recrawl_max），
        # 再下载所有「引用但本地没有」的图片。即使本轮没有新通知也要跑。
        await self._recrawl_images()
        await self._prefetch_images(push)
        # 记录被 retention 淘汰后，它的置顶标记也要清掉（否则列表会显示"已置顶
        # 但查不到正文"的幽灵条目）。只在真有变化时写盘。
        dropped = self._store.prune_pins()
        if dropped:
            self.logger.info("[bupt_news] 清理了 %d 条失效置顶", dropped)

        now = datetime.now()

        # ---- 免打扰：窗口内只落库不推送 ----
        if self._in_quiet_hours(now):
            start, end = self._quiet_window()
            self.logger.info(
                "[bupt_news] 处于免打扰时段（%s-%s），%d 条已入库待补发",
                start.strftime("%H:%M"), end.strftime("%H:%M"),
                len(self._backlog_records()))
            return (len(push), 0, 0)

        is_digest = self._is_digest_due(now)

        # ---- 待推送 = 已落库但尚未推送的记录 ----
        # 关键：不能从 `fresh`（未见过）推导，因为免打扰期间这些通知已被标记
        # `seen` 并落库；若从 fresh 推导，积压将永远不会被补发（会静默丢失）。
        pending = self._backlog_records()

        # R5 / A9：当天早报即使没有积压也要发（只发天气行）。
        if not pending and not is_digest:
            self.logger.info("[bupt_news] 无新通知")
            await self._backfill_ai_results()
            return (0, 0, 0)

        # 单轮上限只在非早报时生效；早报由 digest_max_items 控制显示条数
        if pending and not is_digest:
            cap = max(1, int(self.config.get("max_push_per_round", 8) or 8))
            if len(pending) > cap:
                self.logger.info(
                    "[bupt_news] 待推送 %d 条超出单轮上限 %d，仅推送最新 %d 条",
                    len(pending), cap, cap)
                pending = pending[-cap:]

        # 给本轮摘要一个时间预算：到点就用已有结果渲染（缺的降级为标题截断），
        # 不能让早报等着 AI 一次次重试。
        push_deadline = (time.monotonic() + self._push_budget()
                         if self._push_budget() > 0 else None)
        summaries = (await self._summarize_records(pending, deadline=push_deadline)
                     if pending else {})

        if is_digest:
            desc = await self._weather.today() if self._weather else None
            head = weather_line(
                desc, (self.config.get("weather") or {}).get("label", "北京"), now)
            chain = self._build_digest(head, pending, summaries)
        else:
            chain = self._build_list(pending, summaries)

        sent, failed = await self._push_chain(chain, preview_user=preview_user,
                                              preview_group=preview_group,
                                              records=pending)
        if sent and preview_user is None:
            if pending:
                self._store.note_push(len(pending))
                self._store.mark_pushed(
                    [r["key"] for r in pending if r.get("key")])
            if is_digest:
                self._store.note_digest(self._today(now))
        # 补试历史缺失的 AI 结果放在**推送之后**：它是后台维护任务，
        # 绝不能挡在时间敏感的推送（尤其早报）前面 —— 曾经放在推送前，
        # 结果早报被 60 条学生判定的网络等待拖住、再被进程重启打断，当天早报没发出去。
        await self._backfill_ai_results()
        return (len(pending), sent, failed)

    def _backlog_records(self) -> list[dict]:
        """已落库但尚未推送的记录，按 id 升序（旧 → 新）。

        这就是免打扰期间攒下的积压；`/buptnews sync preview` 等手动路径同样走它。
        """
        out: list[dict] = []
        for rec in self._store.all_records():
            key = rec.get("key")
            if key and not self._store.is_pushed(key):
                out.append(rec)
        return out

    async def _fill_bodies(self, notices: list[Notice]) -> None:
        assert self._portal is not None
        for n in notices:
            try:
                d = await self._portal.fetch_detail(n.url)
                n.body = (d.get("body") or "").strip()
                n.dept = n.dept or d.get("dept", "")
                n.date = n.date or d.get("date", "")
                n.images = list(d.get("images") or [])
                n.tables = [str(t) for t in (d.get("tables") or [])]
            except PortalAuthError:
                raise
            except Exception as e:
                self.logger.warning("[bupt_news] 获取正文失败（%s）：%s", n.title[:24], e)
            await asyncio.sleep(0.2)

    async def _prefetch_images(self, fresh: list[Notice] | None = None,
                               limit: int | None = None, *, force: bool = False,
                               only_failed: bool = False) -> dict:
        """下载「记录引用但本地没有」的图片（本轮新通知优先，其余按记录新→旧）。

        上限默认取 `images.prefetch_max`（0 = 不限），可用 `limit` 覆盖；
        没补完的下一轮继续，日志给出剩余张数。失败只记日志，不影响同步。
        """
        conf = self.config.get("images") or {}
        if self._images is None or not self._images.enabled:
            return dict(EMPTY_DOWNLOAD_STATS)
        if not conf.get("prefetch", True) and limit is None:
            return dict(EMPTY_DOWNLOAD_STATS)
        if limit is None:
            try:
                limit = int(conf.get("prefetch_max", 30) or 0)
            except (TypeError, ValueError):
                limit = 30
        fresh_urls = [str((it or {}).get("url") or "")
                      for n in (fresh or []) for it in (n.images or [])]
        try:
            retry = int(conf.get("retry", 2) or 0)
            retry_delay = float(conf.get("retry_delay", 2.0) or 0)
            max_attempts = int(conf.get("max_attempts", 5) or 0)
        except (TypeError, ValueError):
            retry, retry_delay, max_attempts = 2, 2.0, 5
        stats = await download_missing_images(
            self._store, self._images, fresh_urls=fresh_urls, limit=limit,
            failures=self._img_failures, retry=retry, retry_delay=retry_delay,
            max_attempts=max_attempts, force=force, only_failed=only_failed,
            logger=self.logger)
        if stats["tried"]:
            self.logger.info(
                "[bupt_news] 图片缓存补齐：成功 %d / 失败 %d（本轮尝试 %d 张，"
                "跳过 %d 张，仍有 %d 张待补）",
                stats["ok"], stats["failed"], stats["tried"], stats["skipped"],
                stats["remaining"])
        return stats

    async def _recrawl_images(self, limit: int | None = None) -> dict:
        """给「加图片功能之前入库」的历史记录重抓正文页，补出图片位置。

        上限默认取 `images.recrawl_max`（0 = 不限），受 `images.recrawl` 开关控制。
        """
        conf = self.config.get("images") or {}
        empty = {"targets": 0, "tried": 0, "updated": 0, "failed": 0,
                 "no_images": 0, "no_tables": 0, "remaining": 0}
        if self._images is None or self._portal is None:
            return empty
        if limit is None:
            if not conf.get("recrawl", True):
                return empty
            try:
                limit = int(conf.get("recrawl_max", 50) or 0)
            except (TypeError, ValueError):
                limit = 50
        stats = await recrawl_missing_images(self._store, self._portal,
                                             limit=limit, logger=self.logger)
        if stats["tried"]:
            self.logger.info(
                "[bupt_news] 历史记录补素材：重抓 %d 条（无图 %d 条 / 无表 %d 条 / "
                "失败 %d 条），剩余 %d 条",
                stats["updated"], stats["no_images"], stats.get("no_tables", 0),
                stats["failed"], stats["remaining"])
        return stats

    def _prune_images(self) -> None:
        """按「当前保留的记录仍在引用的素材」清理缓存，避免目录无限增长。

        图片与表格图片一起清：表格图片按**内容哈希**命名，这里用同一条记录的
        `tables` 重新算出仍在用的文件名。
        """
        if self._images is None and self._tables is None:
            return
        keep: list[str] = []
        keep_tables: list[str] = []
        for rec in self._store.all_records():
            for it in rec.get("images") or []:
                if isinstance(it, dict) and it.get("url"):
                    keep.append(str(it["url"]))
            if self._tables is not None:
                for html in rec.get("tables") or []:
                    if html:
                        keep_tables.append(self._tables.path_for(str(html)))
        try:
            if self._images is not None:
                self._images.prune(keep)
            if self._tables is not None:
                removed = self._tables.prune(keep_tables)
                if removed:
                    self.logger.info("[bupt_news] 清理了 %d 个不再引用的表格图片",
                                     removed)
        except Exception as e:  # noqa: BLE001
            self.logger.warning("[bupt_news] 素材缓存清理失败：%s", e)

    # ------------------------------------------------------------------ 摘要 / 推送

    # ---- AI 摘要（带重试）----

    def _summary_cfg(self, key: str, default=None):
        """读 `summarize.<key>`，缺省时退回 `SUMMARY_DEFAULTS`。"""
        if default is None:
            default = SUMMARY_DEFAULTS.get(key)
        s = self.config.get("summarize") or {}
        v = s.get(key, default)
        return default if v is None else v

    def _retry_timeout(self) -> int:
        """补试用的单请求超时（默认比 `summarize.timeout` 宽松）。"""
        try:
            return int(self._summary_cfg("retry_timeout", 120) or 120)
        except (TypeError, ValueError):
            return 120

    @staticmethod
    def _is_hopeless_error(e: BaseException) -> bool:
        """这个错误重试也没用吗？

        DNS 解析失败、连接被拒/不可达、请求超时都属于「整条链路不通」，
        立刻重试只会白等退避时间（2s→4s→8s…）。实测网关域名解析不了时，
        一批补试就在这些无意义的等待里耗掉几分钟 —— 而早报排在同一轮同步里。
        """
        names = {cls.__name__ for cls in type(e).__mro__}
        joined = " ".join(names)
        if "ClientConnector" in joined or "Timeout" in joined:
            return True
        # ⚠️ `str(e)` 必须包在 try 里：`aiohttp.ClientConnectorDNSError` 在缺少
        # conn_key 时 `__str__` 自己会抛 AttributeError（`NoneType has no attribute
        # 'ssl'`）—— 那样连"记一条失败日志"都会炸，整轮补试被带崩。
        try:
            text = str(e) or ""
        except Exception:  # noqa: BLE001
            text = ""
        for hint in ("Name or service not known", "Connection refused",
                     "Network is unreachable", "Temporary failure in name resolution"):
            if hint in text:
                return True
        return False

    def _push_budget(self) -> float:
        """本轮推送的 AI 摘要预算（秒），0 = 不限。"""
        try:
            return float(self._summary_cfg("push_budget", 25.0) or 0)
        except (TypeError, ValueError):
            return 25.0

    def _pin_limit(self) -> int:
        """置顶上限（0 = 不限）。"""
        try:
            return max(0, int(self.config.get("pin_limit", 3) or 0))
        except (TypeError, ValueError):
            return 3

    def _pin_evicted_text(self, evicted: dict | None) -> str:
        """被替换掉的置顶项的一句话描述（给用户看）。"""
        if not evicted:
            return ""
        text = self._pin_line(evicted, width=24)
        return f"{text}" if text else f"P{evicted.get('n')}"

    def _retry_budget(self) -> float:
        """补试的总时间预算（秒），0 = 不限。"""
        try:
            return float(self._summary_cfg("retry_budget", 90.0) or 0)
        except (TypeError, ValueError):
            return 90.0

    def _retry_batch_size(self) -> int:
        """补试用的批量大小（默认比 `summarize.batch_size` 小）。"""
        try:
            return max(1, int(self._summary_cfg("retry_batch_size", 6) or 6))
        except (TypeError, ValueError):
            return 6

    async def _summarize_with_retry(self, recs: list[dict], *,
                                    attempts: int | None = None,
                                    reason: str = "同步",
                                    deadline: float | None = None) -> dict:
        """调 AI 摘要 + 学生判定，失败按 `summarize.retry` 次连续重试（指数退避）。

        返回 `{newsid: NoticeDigest}`，只包含**真正成功**的条目 —— 调用方据此
        判断哪些记录还需要重试。

        成功 = 摘要非空 **且** `student` 判定可识别：后者缺失说明模型没按格式
        返回，重试比"猜一个"更靠谱。
        """
        if not recs or self._summarizer is None:
            return {}
        if attempts is None:
            try:
                attempts = int(self._summary_cfg("retry", 5) or 0) + 1
            except (TypeError, ValueError):
                attempts = 6
        attempts = max(1, attempts)
        try:
            delay = float(self._summary_cfg("retry_delay", 2.0) or 0)
        except (TypeError, ValueError):
            delay = 2.0

        def complete(d) -> bool:
            return bool(getattr(d, "summary", "")) and \
                getattr(d, "for_students", None) is not None

        last = ""
        ok: dict = {}                   # 跨次尝试累计：已成功的条目不能因后续失败被丢掉
        for i in range(1, attempts + 1):
            # 只对**还缺的**条目重试：已拿到的摘要不再重复问、也不再为它花钱
            todo = [r for r in recs
                    if str(r.get("newsid", "")) not in ok]
            if not todo:
                break
            if deadline is not None and time.monotonic() >= deadline:
                self.logger.info("[bupt_news] AI 摘要补试超预算，剩余 %d 条留待下一轮",
                                 len(todo))
                break
            try:
                got = await self._summarizer.summarize(todo)
                got = {k: v for k, v in (got or {}).items() if complete(v)}
            except Exception as e:  # noqa: BLE001 - 摘要 best-effort
                got, last = {}, f"{type(e).__name__}: {e}"
                if self._is_hopeless_error(e):
                    self.logger.warning(
                        "[bupt_news] AI 摘要请求链路不通（%s），本轮不再重试", e)
                    break
            before = len(ok)
            # 只认本次请求的那些 newsid，避免 AI 多返回的键混进来
            wanted = {str(r.get("newsid", "")) for r in todo}
            ok.update({k: v for k, v in got.items() if k in wanted})
            missing = [r for r in recs if str(r.get("newsid", "")) not in ok]
            if not missing:
                if i > 1:
                    self.logger.info("[bupt_news] AI 摘要第 %d 次尝试成功（%s，%d 条）",
                                     i, reason, len(recs))
                return ok
            # `last_error` 只在真实 Summarizer 上保证存在；不要假设每个实现都有
            last = getattr(self._summarizer, "last_error", "") or last \
                or "返回内容缺少部分条目"
            if i < attempts:
                # ⚠️ 退避 sleep 必须受截止时间约束：只检查"尝试前"是不够的 ——
                # 累积退避（2s→4s→8s…）会**睡过** deadline，实测 2s 的预算
                # 仍能拖出 30s 的同步耗时。
                if deadline is not None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        self.logger.info(
                            "[bupt_news] AI 摘要预算已用完（%s），剩余 %d 条留待下一轮",
                            reason, len(missing))
                        break
                    delay = min(delay, left)
                self.logger.warning(
                    "[bupt_news] AI 摘要失败（%s，%d/%d 条缺失，第 %d/%d 次）：%s；"
                    "%.1fs 后重试", reason, len(missing), len(recs), i, attempts,
                    last, delay)
                if delay > 0:
                    await asyncio.sleep(delay)
                delay *= 2                      # 指数退避
            elif len(ok) > before:
                self.logger.warning("[bupt_news] AI 摘要部分成功（%s，%d/%d 条）",
                                    reason, len(ok), len(recs))
        if ok:
            # 有部分成功：照样落库（`_summarize_records` 会给缺失项留空待重试）
            self.logger.warning("[bupt_news] AI 摘要重试 %d 次后仍有 %d/%d 条失败：%s",
                                attempts, len(recs) - len(ok), len(recs), last)
            return ok
        self.logger.error("[bupt_news] AI 摘要重试 %d 次仍失败（%s，%d 条）：%s；"
                          "本轮不写降级摘要，下次同步/调取时再试",
                          attempts, reason, len(recs), last)
        return {}

    async def _summarize_records(self, recs: list[dict],
                                 deadline: float | None = None) -> dict:
        """给这批记录回填 AI 摘要 + 学生判定（带重试），返回 {newsid: 摘要文本}。

        返回的字典是**渲染用**的摘要文本，调用方原有的 `summaries` 用法不变；
        学生判定不塞进这个字典（会和 newsid 撞键），而是写回 `recs` 里的记录对象
        —— 记录是可变 dict，下游拿的是同一批对象，直接用 `_for_students_of()` 读。

        关键约定：**只有 AI 真的成功才写库**（`summary_ai=True`）。重试用尽时
        不把降级文本写进 `summary`，这样这条记录下次同步、或用户 `/buptnews get`
        调取它时还能被识别为"待补摘要"并重试。
        """
        out: dict = {}
        updates: dict = {}
        todo: list[dict] = []
        for r in recs:
            newsid = str(r.get("newsid", ""))
            if r.get("summary") and r.get("summary_ai") and r.get("student_ai"):
                out[newsid] = r["summary"]      # 摘要 + 判定都有，不重复花钱
            else:
                todo.append(r)

        started = time.monotonic()
        got = await self._summarize_with_retry(
            todo, reason="同步", deadline=deadline) if todo else {}
        if todo:
            self.logger.info("[bupt_news] AI 摘要耗时 %.1fs（%d 条成功 / %d 条请求）",
                             time.monotonic() - started, len(got), len(todo))
        for r in recs:
            newsid = str(r.get("newsid", ""))
            d = got.get(newsid)
            if d is not None:
                out[newsid] = d.summary
                # 一并更新内存里的记录：调用方随后就用它渲染/决定是否 @all
                r["summary"], r["summary_ai"] = d.summary, True
                r["for_students"], r["student_ai"] = bool(d.for_students), True
                updates[r["id"]] = {"summary": d.summary, "summary_ai": True,
                                    "for_students": bool(d.for_students),
                                    "student_ai": True}
            elif newsid not in out:
                # 降级文本只用于**本次渲染**，不落库（落库就等于永久放弃重试）
                out.setdefault(newsid, fallback_summary(r.get("title", "")))
        if updates:
            self._store.update_notices(updates)
        return out

    @staticmethod
    def _for_students_of(rec: dict) -> bool:
        """这条通知是否发给学生。

        AI 没给出判定（`student_ai` 缺失）时按 **True** 处理 —— 拿不准时宁可多
        @一次全体成员，也不要漏掉真正发给学生的通知。
        """
        if not rec.get("student_ai"):
            return True
        return bool(rec.get("for_students", True))

    def _any_for_students(self, recs: list[dict]) -> bool:
        """这批推送里是否**至少有一条**发给学生的通知（决定要不要 @全体成员）。"""
        return any(self._for_students_of(r) for r in recs)

    async def _summarize_notice(self, rec: dict, *, external: bool = False,
                                reason: str = "") -> bool:
        """给**单条**通知补 AI 摘要（本地记录或非本地通知都走这里）。

        交互路径要快，所以用 `summarize.retry_on_notice`（默认 2 次）而不是同步那 5 次。
        成功后写回：本地记录进 notices.jsonl；非本地通知进
        `external_notices` 缓存**以及**它的置顶快照（`update_external_notice`）。

        摘要不可得时保持 `summary_ai=False`（调用方据此不显示🤖行），
        绝不写入"标题截断"这种假摘要。
        """
        if self._summarizer is None or not self._summarizer.enabled:
            return False
        if not str(rec.get("newsid", "")):
            # 没有 newsid 就没法对应到 AI 的返回（它按 newsid 回包）
            return False
        try:
            attempts = int(self._summary_cfg("retry_on_notice", 2) or 0) + 1
        except (TypeError, ValueError):
            attempts = 3
        if not reason:
            reason = ("外部 " + str(rec.get("external_id") or "")) if external \
                else f"调取 #{rec.get('id')}"
        got = await self._summarize_with_retry([rec], attempts=attempts,
                                               reason=reason)
        d = got.get(str(rec.get("newsid", "")))
        if d is None:
            return False
        rec["summary"] = d.summary
        rec["summary_ai"] = True
        rec["for_students"] = bool(d.for_students)
        rec["student_ai"] = True
        if external:
            fields = dict(rec)
            fields.setdefault("external_id", external_code(rec))
            self._store.update_external_notice(fields)
        elif rec.get("id") is not None:
            self._store.update_notice(rec["id"], summary=d.summary,
                                      summary_ai=True,
                                      for_students=bool(d.for_students),
                                      student_ai=True)
        return True

    async def _retry_summary_for(self, rec: dict) -> bool:
        """本地记录的单条摘要补试（保留旧入口，语义不变）。"""
        return await self._summarize_notice(rec, external=False)

    async def _summarize_external(self, notice: dict) -> dict:
        """给非本地通知补摘要，返回**改好的那份 dict**（无论成功与否）。

        置顶门户结果时用：编号是 E 开头、内容来自门户，本地库里没有它，
        所以摘要要直接写进通知本身 + 置顶快照。
        """
        rec = dict(notice or {})
        if rec.get("summary_ai") and rec.get("summary"):
            return rec
        try:
            await self._summarize_notice(rec, external=True, reason="置顶外部通知")
        except Exception as e:  # noqa: BLE001 - 摘要失败不该让置顶失败
            self.logger.warning("[bupt_news] 外部通知摘要失败：%s", e)
        return rec

    async def _backfill_ai_results(self) -> int:
        """补试的**统一入口**（每轮同步最多调用一次，且必须在推送之后）。

        包装起来是为了：异常不许影响推送、调用点唯一（重复调用会让 API 调用翻倍）。
        """
        try:
            return await self._retry_missing_summaries()
        except Exception as e:  # noqa: BLE001 - 补试失败不能影响同步
            self.logger.warning("[bupt_news] 补试 AI 结果出错：%s", e)
            return 0

    async def _retry_missing_summaries(self) -> int:
        """补试历史记录里缺 AI 结果的条目。返回本轮补成功条数。

        两类待补记录走**不同路径**：

        - 缺学生判定但**摘要已有** → 只问判定（`summarize_students`，标题就够，
          省掉重新摘要的 token，也不会把已发出去的摘要改写成另一个版本）；
        - 真缺摘要 → 走 `_summarize_records`（带重试，摘要 + 判定一起拿）。

        节流：上一轮**一条都没补成功**时进入冷却（`retry_cooldown`，默认 30 分钟），
        否则每轮同步都会对同一条失败记录重复烧 API（同步间隔只有 10 分钟）。
        只要还有进展（补成功过）就继续下一批，不做无谓等待。
        """
        if self._summarizer is None or not self._summarizer.enabled:
            return 0
        try:
            limit = int(self._summary_cfg("retry_scan_max", 60) or 0)
        except (TypeError, ValueError):
            limit = 60
        pending = [r for r in self._store.all_records()
                   if str(r.get("newsid", ""))
                   and not (r.get("summary_ai") and r.get("student_ai"))]
        if not pending:
            # 不要在这里重置冷却：这个时间戳只用来防止「对同一条失败记录反复烧 API」，
            # 待补清空时让它自然过期即可。此前在这里清零，导致「冷却中恰好没有待补记录」
            # 会把冷却提前解除，下一轮立刻又发起请求（测试里就是这么暴露出来的）。
            return 0
        try:
            cooldown = float(self._summary_cfg("retry_cooldown", 1800.0) or 0)
        except (TypeError, ValueError):
            cooldown = 1800.0
        now = time.time()
        # 统一在这里初始化：实例可能不经 on_load 构造（测试/嵌入用法）
        self._summary_retry_next_at = getattr(self, "_summary_retry_next_at", 0.0)
        if now < self._summary_retry_next_at:
            return 0                          # 冷却中：上轮一条都没补上
        if limit:
            pending = pending[-limit:]          # 优先补最近的

        need_summary = [r for r in pending if not r.get("summary_ai")]
        only_judge = [r for r in pending if r.get("summary_ai")
                      and not r.get("student_ai")]
        self.logger.info("[bupt_news] 补试 AI 结果：%d 条缺摘要 / %d 条只缺学生判定"
                         "（待补共 %d 条，预算 %.0fs）",
                         len(need_summary), len(only_judge), len(pending),
                         self._retry_budget())

        deadline = time.monotonic() + self._retry_budget() \
            if self._retry_budget() > 0 else None

        ok = 0
        if need_summary:
            got = await self._summarize_records(need_summary, deadline=deadline)
            ok += sum(1 for r in need_summary if str(r.get("newsid", "")) in got
                      and (self._store.get_notice(r["id"]) or {}).get("summary_ai"))
        if only_judge and not (deadline and time.monotonic() >= deadline):
            ok += await self._judge_only(only_judge, deadline=deadline)
        elif only_judge:
            self.logger.info("[bupt_news] 补试预算已用完，剩余 %d 条留待下一轮",
                             len(only_judge))

        if ok:
            self.logger.info("[bupt_news] 补试 AI 结果成功 %d/%d 条", ok, len(pending))
            self._summary_retry_next_at = 0.0   # 有进展 → 下一轮继续补（见下方说明）
        else:
            self._summary_retry_next_at = now + cooldown
            self.logger.warning(
                "[bupt_news] 补试 AI 结果未成功（%d 条），%.0f 分钟后再试",
                len(pending), cooldown / 60.0)
        return ok

    async def _judge_only(self, recs: list[dict],
                          deadline: float | None = None) -> int:
        """只补学生判定（摘要已在），返回补成功条数。

        `deadline` 是 `time.monotonic()` 时间戳：每批之前检查，超了就收工，
        不把整轮同步拖住。
        """
        try:
            attempts = int(self._summary_cfg("retry", 5) or 0) + 1
        except (TypeError, ValueError):
            attempts = 6
        try:
            delay = float(self._summary_cfg("retry_delay", 2.0) or 0)
        except (TypeError, ValueError):
            delay = 2.0
        # 只问判定的能力是可选的（测试替身/旧实现可能没有）→ 退回完整摘要
        judge = getattr(self._summarizer, "summarize_students", None)
        if judge is None:
            got = await self._summarize_records(recs)
            return sum(1 for r in recs if str(r.get("newsid", "")) in got
                       and (self._store.get_notice(r["id"]) or {}).get("student_ai"))

        # 实现是否支持 timeout/batch_size 覆盖 —— 先探测签名，
        # **不要**用 `except TypeError` 兜底：那会把实现内部的真实 TypeError
        # 也当成"不支持覆盖"而掩盖掉（测试替身就踩到过这个坑）。
        try:
            _params = inspect.signature(judge).parameters
            supports_overrides = ("timeout" in _params or "batch_size" in _params)
        except (TypeError, ValueError):
            supports_overrides = False

        updates: dict = {}
        for i in range(1, max(1, attempts) + 1):
            todo = [r for r in recs if not r.get("student_ai")]
            if not todo:
                break
            if deadline is not None and time.monotonic() >= deadline:
                self.logger.info("[bupt_news] 学生判定补试超预算，剩余 %d 条留待下一轮",
                                 len(todo))
                break
            try:
                if supports_overrides:
                    # 补试是后台任务，不赶推送时间：放宽超时、缩小批量，
                    # 避免网关较慢时整批超时白丢（实测 10 条/批会 60s 超时）
                    got = await judge(todo, timeout=self._retry_timeout(),
                                      batch_size=self._retry_batch_size())
                else:
                    got = await judge(todo)
            except Exception as e:  # noqa: BLE001
                got = {}
                self.logger.warning("[bupt_news] 学生判定出错：%s", e)
                if self._is_hopeless_error(e):
                    self.logger.warning("[bupt_news] 学生判定链路不通，本轮不再重试")
                    break
            wanted = {str(r.get("newsid", "")) for r in todo}
            before = len(updates)
            for r in todo:
                nid = str(r.get("newsid", ""))
                if nid in got and nid in wanted:
                    r["for_students"] = bool(got[nid])
                    r["student_ai"] = True
                    updates[r["id"]] = {"for_students": bool(got[nid]),
                                        "student_ai": True}
            if len(updates) == before:
                # 本轮一条都没补上 → 立刻放弃，把重试留给下一轮（带冷却）。
                # ⚠️ 不能继续循环：那样一次补试会对同一批重复请求 attempts 次
                # （实测 60 条 × 6 次 = 360 次调用，且每次都整批超时）。
                break
            if delay > 0 and i < attempts:
                await asyncio.sleep(delay)
            delay *= 2
        if updates:
            self._store.update_notices(updates)
        return len(updates)

    def _local_id_for(self, notice) -> int | None:
        """门户结果 → 本地编号：同一通知（`treeid:newsid`）已入库就返回它的 id。

        门户检索结果不在本地库里（可能是很久以前的），所以只能"有则给编号"；
        没有编号时用户可以 `get <链接>` 直接看全文。
        """
        try:
            key = getattr(notice, "key", "")
            newsid = str(getattr(notice, "newsid", ""))
        except Exception:  # noqa: BLE001
            return None
        if not key and not newsid:
            return None
        for rec in reversed(self._store.all_records()):
            if str(rec.get("newsid") or "") == newsid and (
                    not key or f"{rec.get('treeid')}:{newsid}" == key):
                try:
                    return int(rec.get("id"))
                except (TypeError, ValueError):
                    return None
        return None

    async def _portal_search(self, keyword: str, *, page: int, size: int,
                             content: bool) -> tuple[dict | None, str]:
        """门户检索（带短时缓存），返回 `(结果, 错误信息)`。

        缓存同样结果 5 分钟：用户常会"搜一次 → 翻页 → 返回上一页"，
        每次都打门户既慢又容易被限流。门户不可用时返回 `(None, 原因)`，
        调用方降级成只显示本地结果 —— 搜索不该因为门户抖动而整体失败。
        """
        if self._portal is None:
            return None, "门户客户端未就绪"
        key = (keyword, bool(content), int(page), int(size))
        ttl = 300.0
        now = time.time()
        # 实例可能不经 on_load 构造（测试/嵌入用法）→ 惰性建缓存
        if not hasattr(self, "_portal_search_cache"):
            self._portal_search_cache = {}
        hit = self._portal_search_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], ""
        try:
            got = await self._portal.search(
                keyword, page=page, page_size=size, content=bool(content))
        except PortalAuthError:
            return None, "门户会话失效"
        except Exception as e:  # noqa: BLE001 - 门户抖动不该让搜索整体失败
            self.logger.warning("[bupt_news] 门户检索失败（%s）：%s", keyword, e)
            return None, f"{type(e).__name__}: {e}"
        self._portal_search_cache[key] = (now, got)
        if len(self._portal_search_cache) > 64:
            oldest = sorted(self._portal_search_cache.items(),
                            key=lambda kv: kv[1][0])[:16]
            for k, _ in oldest:
                self._portal_search_cache.pop(k, None)
        return got, ""

    @staticmethod
    def _wants_force(raw: str) -> bool:
        """原始命令文本里是否有**裸** `--force` 或 `-f`。

        框架的解析器只认 `--key=value`；裸 `--force` 会被当成位置参数
        （落进 variadic 的 ids），所以这里从原文里识别，命令层据此补上。
        """
        text = str(raw or "")
        return bool(re.search(r"(?:^|\s)(?:--force(?:=\s*(?:true|1|yes|on))?|-[fF])(?=\s|$)",
                              text))

    @staticmethod
    def _strip_force(raw: str) -> str:
        """把裸 `--force` / `-f` 从原文里剥掉（避免它混进位置参数）。"""
        text = str(raw or "")
        return re.sub(r"(?<=\s)(?:--force(?:=\s*(?:true|1|yes|on))?|-[fF])(?=\s|$)",
                      "", text).strip()

    @staticmethod
    def _parse_tokens(raw) -> list[str]:
        """把 `pin 12 E1759-142679` / `pin 12,34` 拆成去重保序的 token 列表。

        与 `_parse_ids` 的区别：这里**保留非数字编号**（`E…`/`P…`），
        由调用方再按类型解析 —— 只用数字解析会把 E 编号直接丢掉。
        """
        if raw is None:
            return []
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        out: list[str] = []
        seen: set[str] = set()
        for chunk in items:
            for part in re.split(r"[,，;；\s]+", str(chunk or "")):
                part = part.strip().lstrip("#").upper()
                if not part:
                    continue
                # 裸 `--force` / `-f` 会被解析器当成位置参数，这里丢掉
                # （真正的 force 由 `_wants_force(raw)` 从原文识别）
                if part in ("--FORCE", "-F") or part.startswith("--FORCE="):
                    continue
                # 纯数字、E 编号、P 编号都收；其它丢弃
                if part.isdigit() or is_external_code(part) or re.fullmatch(
                        r"P\d{1,6}", part):
                    if part not in seen:
                        seen.add(part)
                        out.append(part)
        return out

    def _pin_line(self, entry: dict | None, *, width: int = 40) -> str:
        """置顶列表的一行：**只显示 `P<n>` + 概要**。

        概要 = AI 摘要（有就优先）；没有摘要时退回标题、再不行退回链接尾段。
        刻意**不显示** E 编号 / 日期 / 本地通知编号 —— 置顶区是给人快速扫的，
        编号（E/本地）在需要操作时再说，混在一行里只会把概要挤掉。
        """
        if not entry:
            return ""
        num = entry.get("n")
        if entry.get("ext"):
            notice = entry.get("notice") or {}
            text = (str(notice.get("summary") or "")[:width]
                    if notice.get("summary_ai") else "")
            if not text:
                text = str(notice.get("title") or "")[:width]
            if not text:
                text = entry["ext"]
        else:
            rec = self._store.get_notice(entry.get("id")) or {}
            text = (str(rec.get("summary") or "")[:width]
                    if rec.get("summary_ai") else "")
            if not text:
                text = str(rec.get("title") or "")[:width]
            if not text:
                text = f"#{entry.get('id')}"
        return f"P{num} 📌 {text}"

    async def _pin_one(self, token: str, *, force: bool = False
                       ) -> tuple[str | None, int | None]:
        """把单个 token 置顶，返回 `(显示标签 | None, 置顶编号 | None)`。

        * `None, None` → 无法置顶（编号不存在 / E 编号抓取失败）
        * `标签, None` → 已经在置顶里
        * `标签, n`    → 刚置顶，编号 P<n>
        """
        token = str(token or "").strip().upper()
        if not token:
            return None, None
        if is_external_code(token):
            # 非本地：编号自带链接，先看缓存元数据，没有再抓一次详情
            if self._store.is_pinned_external(token):
                return token, None
            notice = self._store.external_get(token) or {}
            if not notice.get("body"):
                url = external_url(token)
                if self._portal is None:
                    self._pin_fail_reason = "门户未就绪"
                    return None, None
                try:
                    d = await self._portal.fetch_detail(url)
                except Exception as e:  # noqa: BLE001
                    self.logger.warning("[bupt_news] 置顶 %s 时抓取失败：%s", token, e)
                    self._pin_fail_reason = f"抓取失败（{type(e).__name__}）"
                    return None, None
                # ⚠️ 必须把 treeid/newsid 一起带上：外部编号就是从这两个字段
                # 推出来的（external_code），漏了会让编号算成空串、置顶失败。
                pair = decode_external(token) or ("", "")
                notice = {
                    "title": d.get("title") or notice.get("title") or "（无标题）",
                    "dept": d.get("dept") or "", "date": d.get("date") or "",
                    "body": d.get("body") or "", "url": url,
                    "images": d.get("images") or [],
                    "tables": d.get("tables") or [],
                    "treeid": pair[0], "newsid": pair[1],
                    "external_id": token, "student_ai": False,
                }
            pair = decode_external(token) or ("", "")
            notice.setdefault("treeid", pair[0])
            notice.setdefault("newsid", pair[1])
            notice.setdefault("external_id", token)
            # E 编号来自门户、本地库里没有这条：置顶时就补一条 AI 摘要，
            # 让置顶项在 `pin` 列表和 `get P<n>` 里都有摘要可看。
            notice = await self._summarize_external(notice)
            num = self._store.pin_external(
                notice, limit=self._pin_limit(), force=force)
            if num == PIN_LIMIT_REACHED:
                self._pin_fail_reason = "pin_limit"
                return None, None
            return token, num
        try:
            nid = int(token)
        except (TypeError, ValueError):
            return None, None
        if self._store.get_notice(nid) is None:
            self._pin_fail_reason = f"本地库没有 #{nid}"
            return None, None
        if self._store.is_pinned(nid):
            return f"#{nid}", None
        num = self._store.pin(nid, limit=self._pin_limit(), force=force)
        if num == PIN_LIMIT_REACHED:
            self._pin_fail_reason = "pin_limit"
            return None, None
        return f"#{nid}", num

    @staticmethod
    def _parse_ids(raw) -> list[int]:
        """把 `pin 12 34` / `pin 12,34` / `pin ids=12` 里的编号解析成 int 列表。

        `VariadicField` 会把多个编号拆成字符串列表，用户也可能写成逗号分隔、
        中文逗号、或 `#12`（直接照抄帮助里的写法）。这里统一容错并去重保序。

        ⚠️ 不能只取 `raw[0]`：那样 `pin 12 34` 会只置顶 12，而用户以为都置了。
        """
        if raw is None:
            return []
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        out: list[int] = []
        seen: set[int] = set()
        for chunk in items:
            for part in re.split(r"[,，;；\s]+", str(chunk or "")):
                part = part.strip().lstrip("#")
                if not part or not part.isdigit():
                    continue
                nid = int(part)
                if nid > 0 and nid not in seen:
                    seen.add(nid)
                    out.append(nid)
        return out

    def _search_line(self, nid: int, rec: dict, *, keyword: str,
                     prefix: str = "", flag: str = "") -> str:
        """搜索结果行：`📌 编号 [摘要] 标题`。

        标题一定显示 —— 摘要只有 20 字，命中常发生在标题里（"疫苗补种通知"），
        只给摘要用户根本看不出为什么这条被搜出来。
        """
        title = str(rec.get("title") or "")
        summary = (str(rec.get("summary") or "")
                   if rec.get("summary_ai") else "")
        parts = [f"{prefix}{nid} {flag}".rstrip()]
        if summary:
            parts.append(f"[{summary[:16]}]")
        parts.append(title[:40] or "（无标题）")
        return " ".join(parts)

    @staticmethod
    def _search_snippet(rec: dict, keyword: str, width: int = 34) -> str:
        """命中在正文里时给一段上下文；命中标题/摘要时不重复（那行本身就显示了）。"""
        kw = (keyword or "").strip().lower()
        if not kw:
            return ""
        title = str(rec.get("title") or "")
        summary = str(rec.get("summary") or "") if rec.get("summary_ai") else ""
        # 标题很短时整条都显示得下，不需要额外片段；
        # 标题长到被 `_search_line` 截断时，补一段标题上下文才看得到命中词。
        if kw in summary.lower():
            return ""
        if kw in title.lower() and len(title) <= 40:
            return ""
        body = str(rec.get("body") or "")
        idx = body.lower().find(kw)
        if idx < 0:
            return ""
        start = max(0, idx - width // 3)
        end = min(len(body), idx + len(kw) + width)
        piece = re.sub(r"\s+", " ", body[start:end]).strip()
        return ("…" if start > 0 else "") + piece + ("…" if end < len(body) else "")

    def _rec_line(self, nid: int, *, prefix: str = "", width: int = 24) -> str:
        """列表/搜索/置顶共用的一行：`编号 摘要（非学生标记）`。

        三个入口必须同口径 —— 分开写很容易出现「list 里有 ⚠️非学生、search 里没有」
        这类不一致（AI 判定是同一份数据）。
        """
        rec = self._store.get_notice(nid) or {}
        # 只认 AI 摘要：降级文本不落库，所以这里自然显示标题
        text = (rec.get("summary") if rec.get("summary_ai") else "") \
            or rec.get("title") or ""
        flag = "" if self._for_students_of(rec) else "⚠️非学生 "
        return f"{prefix}{nid} {flag}{text[:width]}"

    def _summary_of(self, rec: dict, summaries: dict) -> str:
        """渲染用摘要：优先本次 AI 结果，其次库里已有摘要，最后降级为标题截断。"""
        newsid = str(rec.get("newsid", ""))
        stored = rec.get("summary") if rec.get("summary_ai") else None
        return (summaries.get(newsid)
                or stored
                or fallback_summary(rec.get("title", "")))

    def _split_chain(self, chain: MessageChain):
        """内容过长时按配置切成多条消息（返回单条 chain 或 chain 列表）。"""
        c = self.config.get("chunk") or {}
        chunks = chain.split(max_chars=int(c.get("max_chars", 1200) or 0),
                             max_images=int(c.get("max_images", 4) or 0))
        return chunks[0] if len(chunks) == 1 else chunks

    def _build_list(self, recs: list[dict], summaries: dict) -> MessageChain:
        """白天推送：每行 `#ID 摘要`（不带部门/日期/链接，见规格 §4.1）。

        全部条目都「不是发给学生」时，在消息最前面加一行提示（与 @全体成员 一起收敛）。
        """
        lines = [f"{r['id']} {self._summary_of(r, summaries)}" for r in recs]
        head = "⚠️⚠️⚠️"
        if recs and not self._any_for_students(recs):
            head = f"{_NOT_STUDENT_HINT}\n{head}"
        return MessageChain().text(head + "\n" + "\n".join(lines))

    async def _render_notice(self, rec: dict, *, prefix: str = "",
                             retry_summary: bool = True) -> list:
        """把一条通知渲染成消息（本地记录与门户临时记录共用）。

        `prefix` 是标题前缀（`#476 ` 或 `🔗 `）；`rec` 里 `id` 可缺省
        （门户临时记录没有本地编号）。
        """
        limit = max(0, int(self.config.get("body_max_chars", 0) or 0))
        body = (rec.get("body") or "").strip()
        has_id = rec.get("id") is not None
        # 这条还没有 AI 摘要 / 学生判定 → 本次调取就重试
        # （同步时重试用尽的记录会在这里补上）。门户临时记录没有本地 id，
        # 写不回库，也就没有"补摘要"可言。
        if retry_summary and has_id and not (rec.get("summary_ai")
                                            and rec.get("student_ai")):
            await self._retry_summary_for(rec)
        head = f"#{rec['id']} " if has_id else ""
        header = [f"📢 {prefix}{head}{rec.get('title', '')}"]
        meta = " · ".join(x for x in (rec.get("dept"), rec.get("date")) if x)
        if meta:
            header.append(f"🏛 {meta}")
        # 只显示**真正的 AI 摘要**：门户临时记录没有摘要，降级文本就是标题截断，
        # 显示出来只是把标题重复一遍（"🤖 工作通知 | 关于2025级…"），很误导。
        if rec.get("summary_ai") and rec.get("summary"):
            header.append(f"🤖 {rec['summary']}")
        if not self._for_students_of(rec):
            header.append(_NOT_STUDENT_HINT)
        header.append("────────────")
        tail: list[str] = []
        if has_id and "images" not in rec:
            # 补图功能上线前入库的记录：说清楚为什么没有图，并给出补齐入口
            tail.append("🖼 本条入库时尚未补图，"
                        "发 /buptnews images 查看/补齐")
        footer = f"🔗 原文（含附件）：{rec.get('url', '')}"

        # 正文素材：图片 + 表格（混排，按 pos 定位）
        media = [it for it in (rec.get("images") or [])
                 if isinstance(it, dict)
                 and (it.get("url") or it.get("table") is not None)]
        tables = [str(t) for t in (rec.get("tables") or [])]
        if self._images is None or not self._images.enabled:
            media = [it for it in media if it.get("table") is not None]
        if self._tables is None or not self._tables.enabled:
            media = [it for it in media if it.get("table") is None]

        # 有素材时**不截断正文**：截断点之后的素材会被一起丢掉（补了等于白补），
        # 长文交给 chunk 分段发送。只有纯文字通知才按 body_max_chars 截断。
        truncated = bool(limit and len(body) > limit and not media)
        if truncated:
            body = body[:limit].rstrip() + "…"
        elif media and limit and len(body) > limit:
            self.logger.info(
                "[bupt_news] get %s：有 %d 个正文素材，跳过正文截断（body_max_chars=%d）",
                rec.get("id"), len(media), limit)

        chain = await self._build_notice_chain(
            header, body, media, footer, truncated=truncated,
            cut=limit if truncated else None, notes=tail or None,
            tables=tables)
        return self._split_chain(chain)
    async def _ensure_table_image(self, index: int, tables: list) -> str | None:
        """把第 `index` 个表格渲染成图片并返回本地路径（缓存命中直接返回）。

        渲染前先过一道 `is_renderable()`：门户页面里混着"打印"工具栏表、
        纯布局空表，渲染它们只会往消息里塞废图。
        """
        if self._tables is None or not tables:
            return None
        if not (0 <= index < len(tables)):
            return None
        html = str(tables[index] or "")
        if not html:
            return None
        limit = int((self.config.get("tables") or {}).get("max_per_notice", 5) or 0)
        if limit and index >= limit:
            return None
        try:
            table = parse_table(html)
        except Exception as e:  # noqa: BLE001 - 解析失败不该影响发送
            self.logger.warning("[bupt_news] 表格解析失败：%s", e)
            return None
        if table is None or not is_renderable(table):
            return None
        try:
            return await self._tables.ensure(html)
        except Exception as e:  # noqa: BLE001
            self.logger.warning("[bupt_news] 表格渲染出错：%s", e)
            return None

    async def _build_notice_chain(self, header: list[str], body: str, images: list[dict],
                                  footer: str, *, truncated: bool = False,
                                  cut: int | None = None,
                                  notes: list[str] | None = None,
                                  tables: list[str] | None = None) -> MessageChain:
        """把通知正文按原页面顺序拼成「文字段 + 图片/表格段」的消息。

        `images[].pos` 是素材在正文中的字符偏移：文本切到该位置、插入素材、再继续，
        因此消息里的顺序与原文一致（即原排版）。素材有两种：

        * `{"pos", "url"}`   图片 → 下载/读缓存后发送；
        * `{"pos", "table"}` 表格 → 用 `tables[下标]` 的原始 HTML 渲染成图片再发送
          （正文文本里的表格已被拍平，行列关系只能靠图片还原）。

        素材缺失/渲染失败会被跳过并在末尾说明，正文不受影响。
        """
        # 不能 assert：图片缓存可能未初始化（插件未 enable / 嵌入用法 / 测试），
        # 那种情况下应该照发正文，而不是让整条通知渲染失败。
        tables = tables or []
        images_ready = self._images is not None
        chain = MessageChain().text("\n".join(header))
        body_started = False
        missed = 0
        missed_tables = 0

        # 先按图片偏移把正文切成 "text / img / text / img / …"
        ordered = sorted(images, key=lambda it: int(it.get("pos", 0) or 0))
        items: list[tuple[str, str]] = []
        cursor = 0
        for it in ordered:
            pos = max(0, min(int(it.get("pos", 0) or 0), len(body)))
            if cut is not None and pos >= cut:
                continue                      # 落在被截断的正文之后 → 不发送
            if it.get("table") is not None:
                items.append(("text", body[cursor:pos]))
                items.append(("table", str(it["table"])))
                cursor = pos
                continue
            if not it.get("url"):
                continue
            items.append(("text", body[cursor:pos]))
            items.append(("img", str(it["url"])))
            cursor = pos
        items.append(("text", body[cursor:]))

        # 图片前后各留一个换行：去掉正文在图片处本就带的空行，避免出现大段空白
        for i, (kind, _) in enumerate(items):
            if kind not in ("img", "table"):
                continue
            if i > 0 and items[i - 1][0] == "text":
                items[i - 1] = ("text", items[i - 1][1].rstrip("\n"))
            if i + 1 < len(items) and items[i + 1][0] == "text":
                items[i + 1] = ("text", items[i + 1][1].lstrip("\n"))

        for kind, val in items:
            if kind == "table":
                # 表格：原始 HTML → 图片。渲染失败就退回文本形式（正文里本就有拍平的文字）
                path = await self._ensure_table_image(int(val), tables)
                if path:
                    chain.text("\n").image(file=Path(path).as_uri(),
                                           name=os.path.basename(path))
                    body_started = True
                else:
                    missed_tables += 1
                continue
            if kind == "img":
                path = await self._images.ensure(val) if images_ready else None
                if path:
                    # 用 file:// 引用本地缓存文件（OneBot 本地图片写法，见 docs/usage.md）
                    chain.text("\n").image(file=Path(path).as_uri(),
                                           name=os.path.basename(path))
                    body_started = True
                else:
                    missed += 1
                continue
            if not val:
                continue
            prefix = "" if body_started else "📄 "
            chain.text(f"\n{prefix}{val}")
            body_started = True

        if truncated:
            chain.text("\n…")
        if missed:
            chain.text(f"\n（{missed} 张图片未获取到，见原文）")
        if missed_tables:
            chain.text(f"\n（{missed_tables} 个表格未能渲染成图片，"
                       "请点原文查看完整表格）")
        for note in (notes or []):
            chain.text(f"\n{note}")
        chain.text(f"\n{footer}")
        return chain

    def _build_digest(self, head: str, recs: list[dict],
                      summaries: dict) -> MessageChain:
        """早报：天气行 + 条目列表（超限截断）。

        没有条目时**只发天气行**（规格 R5/A9，别加标题/提示）。
        全部条目都「不是发给学生」时，在最后加一行提示（与不 @全体成员 一致）。
        """
        if not recs:
            return MessageChain().text(head)
        cap = max(1, int(self.config.get("digest_max_items", 40) or 40))
        shown = recs if len(recs) <= cap else recs[-cap:]
        hidden = len(recs) - len(shown)
        lines = [head, "", f"📋 今日通知（{len(recs)} 条）"]
        lines += [f"{r['id']} {self._summary_of(r, summaries)}" for r in shown]
        if hidden > 0:
            lines.append(f"…另有 {hidden} 条")
        if not self._any_for_students(recs):
            lines += ["", _NOT_STUDENT_HINT]
        lines += ["", "💡 /buptnews get <编号> 查看全文 · /buptnews list 翻页"]
        return MessageChain().text("\n".join(lines))

    # ---- @全体成员 ----

    async def _at_all_quota(self, groups: list[int]) -> dict:
        """推送前查询每个群「能否 @全体成员」以及「还剩几次」。

        返回 `{群号: _AtAllQuota}`（详见该类的说明）。查询失败的群得到
        `can_at_all=None`，既不 @all 也不提示。
        """
        if not self.config.get("at_all", True):
            return {g: _AtAllQuota(None) for g in groups}
        out = {}
        for gid in groups:
            try:
                data = await self.api.get_group_at_all_remain(str(gid))
                quota = _parse_at_all_remain(data)
                out[gid] = quota
            except Exception as e:
                self.logger.warning("[bupt_news] 查询群 %s 的 @全体成员 次数失败：%s",
                                    gid, e)
                out[gid] = _AtAllQuota(None)
            else:
                self.logger.info("[bupt_news] 群 %s @全体成员：%s（原始返回 %s）",
                                 gid, quota.summary, data)
        return out

    @staticmethod
    def _prefix_at_all(chain: MessageChain) -> MessageChain:
        """把 @全体成员 放在消息最前面。"""
        return MessageChain().at("all").text("\n") + chain

    async def _push_chain(self, chain: MessageChain,
                          preview_user: int | None = None,
                          preview_group: int | None = None,
                          records: list[dict] | None = None):
        """把已组装好的消息发往配置的目标；过长时按 chunk 配置分段发送。

        `preview_group`：在**群聊临时会话（小窗）**里执行 preview 时带上来源群号，
        否则服务端会当好友私聊发送（非好友发不出去）。
        `records`：本批推送的通知记录，用于判断「是否发给学生」——全都不是给学生的
        时候不 @全体成员。
        """
        split = self._split_chain(chain)
        chunks = split if isinstance(split, list) else [split]

        if preview_user is not None:
            try:
                for ch in chunks:
                    await self.api.send_private_msg(
                        str(preview_user), ch,
                        group_id=str(preview_group) if preview_group else None)
                return 1, 0
            except Exception as e:
                self.logger.error("[bupt_news] 预览发送失败：%s", e)
                return 0, 1

        if not self.config.get("enabled", True):
            self.logger.info("[bupt_news] 推送开关已关闭，跳过发送")
            return 0, 0

        groups = self._int_list("groups")
        users = self._int_list("users")
        if not groups and not users:
            self.logger.warning("[bupt_news] 未配置推送目标，跳过发送")
            return 0, 0

        sent = failed = 0
        # @全体成员 只对**群聊**有意义；私聊不带（带了会显示成一行 "@全体成员"）
        at_all_left = await self._at_all_quota(groups)
        # 这批通知**全都不是给学生的** → 不 @全体成员（@了只会打扰全群），
        # 消息本身已由 `_build_list`/`_build_digest` 加了「疑似不是给学生的通知」提示。
        # 一条都没有（如只有天气行的早报）时不改行为，仍按原逻辑处理。
        all_not_student = bool(records) and not self._any_for_students(records)
        for gid in groups:
            try:
                # ⚠️ 不能用 `or` 兜底：can_at_all=False 的配额对象是真值为假，
                # `... or _AtAllQuota(None)` 会把「已用尽」悄悄换成「不知道」。
                quota = at_all_left.get(gid)
                if quota is None:
                    quota = _AtAllQuota(None)
                # ⚠️ MessageChain 是**可变**对象（.at()/.text() 往同一个对象追加），
                # 所以这里必须为每个群生成独立的分段列表：早期版本把加过 @all 的链
                # 写回共享的 chunks[0]，第二个群就会变成「@all @all 正文」。
                chunks_for_group = list(chunks)
                if quota.can_at_all is True and not all_not_student:
                    chunks_for_group[0] = self._prefix_at_all(chunks[0])
                for ch in chunks_for_group:
                    await self.api.send_group_msg(str(gid), ch)
                if quota.exhausted:
                    # 次数不足：补一条醒目提示，提醒同学注意查收
                    await self.api.send_group_msg(
                        str(gid), MessageChain().text(_AT_ALL_EXHAUSTED_HINT))
                    self.logger.warning("[bupt_news] 群 %s 的 @全体成员 次数已用尽"
                                        "（%s），已追加醒目提示", gid, quota.summary)
                sent += 1
                self.logger.info("[bupt_news] 已推送 → 群 %s（@全体成员=%s，%s）",
                                 gid, "是" if quota.can_at_all is True else "否",
                                 quota.summary)
            except Exception as e:
                failed += 1
                self.logger.error("[bupt_news] 推送群 %s 失败：%s", gid, e)
        for uid in users:
            try:
                # 私聊：原文照发，绝不带 @全体成员
                for ch in chunks:
                    await self.api.send_private_msg(str(uid), ch)
                sent += 1
                self.logger.info("[bupt_news] 已推送 → 用户 %s", uid)
            except Exception as e:
                failed += 1
                self.logger.error("[bupt_news] 推送用户 %s 失败：%s", uid, e)
        return sent, failed

    # ------------------------------------------------------------------ 命令

    def _register_commands(self) -> None:
        self._command_names = []

        def reg(name: str, **kw):
            self._command_names.append(name)
            return self.register_command(name, **kw)

        # 私聊可用范围守卫：包在**每一条**命令（含子命令、含命令组本体）外面。
        # 为什么不是一个自定义权限节点（`perm.register("buptnews", …)`）：权限节点
        # 只有「通过 / 不通过」两态，挂到命令上会把 `list`/`get` 这类**公开**命令
        # 也变成白名单专属权限项，与现有「公开 / admin / superuser」三级声明打架；
        # 这里要的是「服务范围」这道**独立于权限等级**的闸门（与 ai_chat 同构）。
        #
        # 为什么不能只靠插件把某个权限节点设成 superuser：那同样会把公开命令
        # 抬成仅超管，且/perm 还能被改成别的值而悄悄失效。
        def _guarded(func):
            # ⚠️ 这里有两个坑，都是实测踩出来的：
            #
            # 1. **包装器必须自己声明 `packet` 与 `params`。** 框架是
            #    `for name, param in inspect.signature(self.func).parameters`
            #    按**参数名**注入的，而 `functools.wraps` 会把 `__signature__`
            #    指回原函数 —— 于是「多带一个参数」看起来毫无作用：
            #    `cmd_list(params)` 只声明了 params，一个事件都拿不到，从
            #    `**kwargs` 里取 `event` 永远是 None，守卫对这类命令**静默失效**
            #    （实测：非白名单用户照样能 list）。所以显式改
            #    `wrapper.__signature__`，让框架看到的就是包装器的真实参数。
            #    反过来 `event` 绝不能出现在这份签名里：框架会多塞一个 event，
            #    而它不在转发名单里 → 给 `cmd_list(params)` 就成了 TypeError。
            #    `params` 必须声明，否则框架压根不注入它（注入口的 `elif` 链只认
            #    参数名，`**kwargs` 不在 `sig.parameters` 里）→ 原函数收不到参数。
            #    `packet` 是框架**无条件**注入的，且 `CommandManager.handle()`
            #    在调用前就设好了 `packet.event`，取事件只能从它取。
            # 2. 转发时仍按**原函数**签名挑选，但 `packet` / `params` 要**手工放回**：
            #    这两个被包装器自己的形参接住了（框架是按关键字传的），
            #    **不会**出现在 `**kwargs` 里 —— 只挑 kwargs 会把它们全丢掉，
            #    表现为 `cmd_status(packet)` 报 missing argument（实测）。
            #    多转发一个 `packet` 给 `async def cmd(params)` 又会 TypeError，
            #    而它被框架吞成「命令没反应」，所以仍按 `wanted` 过滤。
            wanted = tuple(inspect.signature(func).parameters)

            @functools.wraps(func)
            async def wrapper(packet=None, params=None, **kwargs):
                event = getattr(packet, "event", None)
                if not self.check_access(event):
                    self._log_scope_denied(event)
                    return None               # 静默：与框架隐藏命令的口径一致
                forward = {k: v for k, v in kwargs.items() if k in wanted}
                if "packet" in wanted:
                    forward["packet"] = packet
                if "params" in wanted and params is not None:
                    forward["params"] = params
                if "event" in wanted and event is not None:
                    # event 也不在 `**kwargs` 里（除非写进 __signature__，而那样
                    # 框架会把它塞给不收 event 的命令 → TypeError），手工补上。
                    forward["event"] = event
                return await func(**forward)

            wrapper.__signature__ = inspect.Signature([
                inspect.Parameter("packet", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=None),
                inspect.Parameter("params", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=None)])
            return wrapper

        # `reg()` 与本文件后续所有 `@reg(...)` 都经这里注册 —— 守卫在**登记处**
        # 统一套上，就不会漏掉后加的子命令（漏一条 = 该命令在私聊里全通）。
        original_register = self.register_command

        def _register_with_guard(name, **kw):
            decorator = original_register(name, **kw)

            def _wrap(func):
                return decorator(_guarded(func))
            return _wrap

        self.register_command = _register_with_guard
        self._install_visibility_filter()

        # 注意：子命令级权限校验已上移到框架（napcat/command.py 的
        # CommandManager.handle() 现在会解析"实际执行的 handler"再校验，
        # 且 register() 支持子命令继承父命令的 permission/scope）。
        # 此前这里曾有一个 require() 绕行，已随 T1 修复一并退役。
        # 这里的 permission="admin" 只是各子命令的**默认值**：带子命令的命令是命令组，
        # 本体不设权限（框架不校验本体），权限一律以各子命令的声明为准。

        @reg("buptnews", permission="admin", scope="all",
             description="校内通知推送管理")
        async def cmd_root(packet):
            # 子命令清单（含每条命令自己的权限与范围）交给框架生成：
            # 手写清单会与注册声明漂移，且带不上权限提示。
            handler = self.cmds.list_commands().get("buptnews")
            if handler is None:                     # 理论上不可达
                return "📢 校内通知推送：子命令清单不可用，见日志"
            # 占位符图例由框架在 render_help 里按需附上（子命令都带参数），
            # 这里不要再追加一次，否则会连着出现两行。
            text = "📢 " + handler.help_text
            evt = getattr(packet, "event", None)
            if evt is not None and evt.message_type == "group":
                text += "\n\n" + GROUP_AT_HINT   # 群里要 @机器人 才会触发
            return text

        @reg("buptnews.status", permission="superuser", scope="private",
             description="查看推送状态")
        async def cmd_status(packet):
            st = self._store
            last = st.last_sync
            last_s = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S") if last else "从未"
            interval = self._interval()
            start, end = self._quiet_window()
            img_files, img_size = self._images.stats() if self._images else (0, 0)
            tbl_files, tbl_size = self._tables.stats() if self._tables else (0, 0)
            on = "✅ 开" if self.config.get("enabled", True) else "⏸ 关"
            return "\n".join([
                "📢 校内通知推送状态",
                "",
                "⚙️ 运行",
                f"  推送：{on}",
                f"  间隔：{interval}s（{interval // 60} 分钟）",
                f"  免打扰：{start.strftime('%H:%M')}–{end.strftime('%H:%M')}",
                "",
                "📬 目标",
                f"  群：{len(self._int_list('groups'))} 个",
                f"  用户：{len(self._int_list('users'))} 个",
                f"  栏目：{', '.join(map(str, self._columns()))}",
                "",
                "📊 记录",
                f"  上次同步：{last_s}",
                f"  上次早报：{st.last_digest_date or '从未'}",
                f"  累计推送：{st.pushed_count} 条",
                f"  可查询：{st.notice_count()} 条（基线 {st.seen_count(SEEN_SCOPE)} 条）",
                "",
                f"🖼 图片缓存：{img_files} 个 / {img_size / 1048576:.1f} MB",
                f"📊 表格图片：{tbl_files} 个 / {tbl_size / 1048576:.1f} MB",
                f"📌 置顶：{self._store.pin_count} 条",
            ])

        # permission="" 是显式覆盖：子命令不继承父命令的 admin 默认值，即无权限限制
        @reg("buptnews.list", permission="", scope="all",
             schema=ListSchema, description="分页列出通知编号（最新在前）")
        async def cmd_list(params):
            total = self._store.notice_count()
            if not total:
                return "📋 暂无通知记录\n💡 等下次同步，或用 /buptnews sync 立即同步"
            size = max(1, int(self.config.get("list_page_size", 20) or 20))
            pages = max(1, -(-total // size))          # ceil
            try:
                want = int(params.page or 1)
            except (TypeError, ValueError):
                want = 1
            page = min(max(1, want), pages)
            newest_first = list(reversed(self._store.recent_ids(total)))
            chunk = newest_first[(page - 1) * size: page * size]
            lines = [f"📋 通知编号（第 {page}/{pages} 页 · 共 {total} 条 · 最新在前）", ""]
            # 置顶条目**每页都显示在最前**：置顶的意义就是"不用翻页也能看到"
            # 置顶区：每行只有 `P<n>` + 概要（外部置顶先补齐摘要再渲染）
            entries = list(reversed(self._store.pinned_entries()))
            if entries:
                for e in entries:
                    if e.get("ext") and not (e.get("notice") or {}).get("summary_ai"):
                        notice = await self._summarize_external(
                            dict(e.get("notice") or {}))
                        if notice.get("summary_ai"):
                            e["notice"] = notice
                lines.append(f"📌 置顶（{len(entries)}）")
                lines += [self._pin_line(e) for e in entries]
                lines.append("")
            for i in chunk:
                lines.append(self._rec_line(i))
            lines.append("")
            nav = []
            if page > 1:
                nav.append(f"⬅️ 上一页：/buptnews list {page - 1}")
            if page < pages:
                nav.append(f"➡️ 下一页：/buptnews list {page + 1}")
            else:
                nav.append("✅ 已是最后一页")
            lines.append(" · ".join(nav))
            lines.append("💡 /buptnews get <编号> 查看全文 · "
                         "/buptnews search <关键词> 搜索")
            return "\n".join(lines)

        @reg("buptnews.pin", permission="superuser", scope="private",
             schema=PinSchema,
             description="置顶通知（编号或 E 编号；不带参数查看当前置顶）")
        async def cmd_pin(params, packet=None):
            # 裸 `--force` / `-f` 由框架当作位置参数，这里从原文补回来
            raw = getattr(packet, "raw", "") if packet is not None else ""
            tokens = self._parse_tokens(getattr(params, "ids", None))
            force = (bool(getattr(params, "force", False))
                     or self._wants_force(raw))
            if not tokens:
                entries = list(reversed(self._store.pinned_entries()))
                if not entries:
                    return ("📌 当前没有置顶通知\n"
                            "💡 /buptnews pin <编号|E编号> 置顶（可多个：pin 12 E1759-142679）")
                lines = [f"📌 置顶通知（{len(entries)}）", ""]
                # 外部置顶若还没摘要（置顶时 AI 恰好不可用），在这里补一次，
                # 补上就立即显示 —— 否则列表里永远只有标题
                for e in entries:
                    if e.get("ext") and not (e.get("notice") or {}).get("summary_ai"):
                        notice = await self._summarize_external(
                            dict(e.get("notice") or {}))
                        if notice.get("summary_ai"):
                            e["notice"] = notice
                lines += [self._pin_line(e) for e in entries]
                lines += ["", "💡 /buptnews unpin P<编号> 取消某条 · "
                              "unpin 清空全部"]
                return "\n".join(lines)

            ok, already, missing, full = [], [], [], []
            for tok in tokens:
                self._pin_fail_reason = ""
                label, num = await self._pin_one(tok, force=force)
                if self._pin_fail_reason == "pin_limit":
                    full.append(tok)
                    continue
                # ⚠️ 先判失败再判 already：成功返回 (label, 编号)，
                # already 返回 (label, None)，失败返回 (None, None)。
                # 反过来写会把"失败"误报成"已经置顶过"。
                if label is None:
                    missing.append(tok)
                elif num is None:
                    already.append(label)
                else:
                    ok.append((label, num))
            lines = []
            if ok:
                lines.append(f"📌 已置顶 {len(ok)} 条：" + "、".join(
                    f"P{n}（{label}）" for label, n in ok))
            if already:
                lines.append(f"ℹ️ 已在置顶中：{'、'.join(already)}")
            if full:
                lines.append(
                    f"⚠️ 置顶已满（上限 {self._pin_limit()} 条），未置顶："
                    f"{'、'.join(full)}\n"
                    "  先 /buptnews unpin P<n> 腾位置，"
                    f"或加 --force 替换最早置顶的那条")
            if missing:
                lines.append(f"❓ 无法置顶：{'、'.join(missing)}"
                             "（本地编号不存在，或 E 编号格式不对）")
            evicted = getattr(self._store, "last_pin_evicted", None)
            if ok:
                lines.append("")
                lines += [self._pin_line(self._store.pin_entry(n))
                          for _, n in ok if self._store.pin_entry(n)]
                if evicted:
                    lines.append(f"\n♻️ 已替换最早置顶的：{self._pin_evicted_text(evicted)}")
                lines.append("\n💡 置顶编号 P1/P2… 独立于通知编号，"
                             "取消后空号会被复用")
            else:
                lines.append("\n💡 /buptnews pin <编号|E编号> 置顶，"
                             "/buptnews unpin P<n> 取消")
            return "\n".join(lines)

        @reg("buptnews.unpin", permission="superuser", scope="private",
             schema=UnpinSchema,
             description="取消置顶（P置顶编号/通知编号/E编号；不带参数=全部取消）")
        async def cmd_unpin(params):
            tokens = self._parse_tokens(getattr(params, "ids", None))
            # 兼容短选项：`-f` 在 schema 里是 kwargs 的 `f`，不会映射到 force
            force = bool(getattr(params, "force", False)) or bool(
                params.get("f", False))
            if not tokens:
                n = self._store.clear_pins()
                return (f"🧹 已取消全部置顶（{n} 条）" if n
                        else "📌 当前没有置顶通知")
            ok, not_pinned = [], []
            for tok in tokens:
                # P 前缀：按**置顶编号**取消（unpin 本身只认通知编号/E 编号，
                # 所以这里要先解析成置顶项再摘）
                entry = self._store.pin_entry(tok) if re.fullmatch(
                    r"P\d{1,6}", tok.upper()) else None
                if entry and self._store.unpin_number(entry["n"]):
                    ok.append(tok)
                elif not entry and self._store.unpin(tok):
                    ok.append(tok)
                else:
                    not_pinned.append(tok)
            lines = []
            if ok:
                lines.append(f"✅ 已取消置顶：{'、'.join(ok)}")
            if not_pinned:
                lines.append(f"ℹ️ 本来就没置顶：{'、'.join(not_pinned)}")
            lines.append(f"\n📌 当前还有 {self._store.pin_count} 条置顶")
            return "\n".join(lines)

        # 搜索：任何私聊用户都能用（与 get 同一条边界）
        @reg("buptnews.search", permission="", scope="private",
             schema=SearchSchema, description="搜索通知（本地库 + 信息门户）")
        async def cmd_search(params):
            kw = str(params.keyword or "").strip()
            if not kw:
                return "❓ 请给一个关键词：/buptnews search <关键词>"
            source = str(getattr(params, "source", "all") or "all").lower()
            want_content = bool(getattr(params, "content", True))
            size = max(1, int(params.limit or 10))
            page = max(1, int(params.page or 1))
            date = str(params.date or "").strip()

            local_hits, local_total = ([], 0)
            if source in ("all", "local"):
                local_hits, local_total = self._store.search(
                    kw, limit=size, page=page, date=date)

            portal = None
            portal_err = ""
            if source in ("all", "portal"):
                portal, portal_err = await self._portal_search(
                    kw, page=page, size=size, content=want_content)

            if not local_total and not (portal and portal.get("total")) and not portal_err:
                hint = "（门户按标题搜）" if not want_content else ""
                dhint = f"（限定日期 {date}）" if date else ""
                return f"🔍 没有找到包含「{kw}」的通知{hint}{dhint}"

            lines = []
            scope = []
            if source in ("all", "local"):
                scope.append(f"本地 {local_total} 条")
            if portal is not None:
                scope.append(f"门户 {portal.get('total', 0)} 条")
            total_hint = local_total + (portal.get("total", 0) if portal else 0)
            lines.append(f"🔍 「{kw}」命中 {total_hint} 条"
                         + (f"（{'／'.join(scope)}）" if scope else "")
                         + f"· 第 {page} 页 · 每页 {size}")
            lines.append("")

            if local_hits:
                lines.append(f"📚 本地库（可 /buptnews get <编号> 看全文）")
                for rec in local_hits:
                    nid = rec.get("id")
                    mark = "📌" if self._store.is_pinned(nid) else "  "
                    flag = "" if self._for_students_of(rec) else "⚠️非学生 "
                    lines.append(self._search_line(
                        int(nid), rec, keyword=kw, prefix=mark + " ", flag=flag))
                    snippet = self._search_snippet(rec, kw)
                    if snippet:
                        lines.append(f"     └ {snippet}")
                lines.append("")

            if portal is not None and portal.get("items"):
                lines.append("🌐 信息门户（含本地库里没有的旧通知）")
                # 给每条门户结果发一个**可解码编号**（E<treeid>-<newsid>），
                # 直接 get/pin 这个编号即可，不用再贴长链接。
                codes = self._store.external_put_many(portal["items"]) \
                    if hasattr(self._store, "external_put_many") else []
                for idx, n in enumerate(portal["items"]):
                    code = codes[idx] if idx < len(codes) else ""
                    local_id = self._local_id_for(n)
                    pinned = (local_id and self._store.is_pinned(local_id)) or \
                        (code and self._store.is_pinned_external(code))
                    mark = "📌" if pinned else "  "
                    date_s = f"{n.date} " if n.date else ""
                    # 本地已有就用本地编号（能进 list/pin 同一套编号空间），
                    # 否则用 E 编号
                    tag = f"#{local_id}" if local_id else (code or "  ")
                    lines.append(f"{mark} {tag} {date_s}{n.title[:38]}")
                lines.append("")
                lines.append("💡 门户结果用 E 编号引用："
                             "get E1759-142679 · pin E1759-142679")

            if portal_err:
                lines.append(f"⚠️ 门户检索失败（已只显示本地结果）：{portal_err}")
                lines.append("")

            nav = []
            if local_total > page * size or (portal and page < portal.get("pages", 0)):
                nav.append(f"➡️ 下一页：/buptnews search {kw} {source} {want_content}"
                           f" {size} {page + 1}")
            if page > 1:
                nav.append(f"⬅️ 上一页：/buptnews search {kw} {source} {want_content}"
                           f" {size} {page - 1}")
            if nav:
                lines.append(" · ".join(nav))
            lines.append("💡 get/pin 支持本地编号、E 编号（门户）与 P 编号（置顶）")
            return "\n".join(lines)

        # permission="" 同样是显式覆盖：仅私聊，但任何私聊用户都能查全文
        @reg("buptnews.get", permission="", scope="private",
             schema=GetSchema,
             description="按编号查看通知全文（本地编号 / E编号 / P置顶编号 / 链接）")
        async def cmd_get(params, event):
            raw = str(getattr(params, "id", "") or "").strip()
            # 支持四种引用：
            #   476                     本地编号
            #   E1759-142679            门户结果编号（**自带** treeid/newsid，可直接还原链接）
            #   P2                      置顶编号
            #   http://…                原文链接
            # 先按置顶编号查（P2），因为没有别的空间用 P 前缀，不会撞车。
            pin_entry = self._store.pin_entry(raw) if raw[:1].upper() == "P" else None
            if pin_entry:
                if pin_entry.get("ext"):
                    notice = dict(pin_entry.get("notice") or {})
                    # 置顶时摘要可能没拿到（AI 暂时不可用）→ 这次调取再试一次
                    notice = await self._summarize_external(notice)
                    return await self._render_notice(
                        notice, prefix=f"P{pin_entry['n']} {pin_entry['ext']} ")
                nid = pin_entry.get("id")
                rec = self._store.get_notice(nid)
                if not rec:
                    return f"❓ 置顶 P{pin_entry['n']} 的通知已超出保留范围"
                return await self._render_notice(
                    rec, prefix=f"P{pin_entry['n']} ")

            if is_external_code(raw):
                # 编号自带链接 → 不依赖任何缓存就能抓取；有缓存元数据则先拿来用
                url = external_url(raw)
                cached = self._store.external_get(raw) or {}
                if self._portal is None:
                    return "门户客户端未就绪，稍后再试"
                try:
                    d = await self._portal.fetch_detail(url)
                except PortalAuthError:
                    return "❌ 门户会话失效，稍后再试"
                except Exception as e:  # noqa: BLE001
                    return f"❌ 打开 {raw} 失败：{type(e).__name__}: {e}"
                rec = {
                    "title": d.get("title") or cached.get("title") or "（无标题）",
                    "dept": d.get("dept") or cached.get("dept") or "",
                    "date": d.get("date") or cached.get("date") or "",
                    "body": d.get("body") or "", "url": url,
                    "images": d.get("images") or [],
                    "tables": d.get("tables") or [],
                    "external_id": raw,
                    # 门户临时记录没有"是否给学生"的 AI 判定 → 按学生处理（不误标）
                    "student_ai": False,
                }
                # E 编号来自门户：正文到手后补一条 AI 摘要（与置顶时同一条路径）
                rec = await self._summarize_external(rec)
                return await self._render_notice(rec, prefix=f"🔗 {raw} ")

            if raw.lower().startswith(("http://", "https://")):
                if self._portal is None:
                    return "门户客户端未就绪，稍后再试"
                try:
                    d = await self._portal.fetch_detail(raw)
                except PortalAuthError:
                    return "❌ 门户会话失效，稍后再试"
                except Exception as e:  # noqa: BLE001
                    return f"❌ 打开链接失败：{type(e).__name__}: {e}"
                rec = {
                    "title": d.get("title") or "（无标题）",
                    "dept": d.get("dept") or "", "date": d.get("date") or "",
                    "body": d.get("body") or "", "url": raw,
                    "images": d.get("images") or [], "tables": d.get("tables") or [],
                    "student_ai": False,
                }
                return await self._render_notice(rec, prefix="🔗 ")

            try:
                nid = int(raw)
            except (TypeError, ValueError):
                return ("❓ 请给编号或链接：/buptnews get <编号|E编号|P置顶编号|链接>\n"
                        "💡 E 编号来自 /buptnews search 的门户结果")
            rec = self._store.get_notice(nid)
            if not rec:
                return (f"❓ 未找到 {nid}"
                        f"（仅保留最近 {self.config.get('notice_retention', 500)} 条）\n"
                        "💡 /buptnews list 看可查询编号 · "
                        "/buptnews search <关键词> 搜门户")
            return await self._render_notice(rec, prefix=f"#{nid} ")


        @reg("buptnews.images", permission="superuser", scope="private",
             schema=ImagesSchema, description="图片缓存：统计 / 补齐历史图片")
        async def cmd_images(params):
            if self._images is None:
                return "图片缓存未初始化"
            action = (params.action or "stats").lower()
            limit = max(0, int(params.limit or 0))
            conf = self.config.get("images") or {}

            if action == "backfill":
                # 指定 id → 只刷新这一条（表格/图片立即就位，不必等批量轮到它）
                if int(params.id or 0) > 0:
                    if self._portal is None:
                        return "门户客户端未就绪，稍后再试"
                    one = await refresh_one_record(
                        self._store, self._portal, int(params.id),
                        logger=self.logger)
                    if not one.get("ok"):
                        return f"❌ 刷新 #{params.id} 失败：{one.get('reason')}"
                    dl = await self._prefetch_images(limit=None, force=True)
                    return (f"✅ 已刷新 #{params.id}："
                            f"图片 {one['images']} 张 / 表格 {one['tables']} 个"
                            f"（新图下载 {dl.get('ok', 0)} 张）\n"
                            f"💡 /buptnews get {params.id} 查看（表格会自动出图）")
                # 重抓「没有 images 字段」的历史记录（默认 recrawl_max 条），再下载缺的图片
                use = limit or max(0, int(conf.get("recrawl_max", 50) or 0))
                rc = await self._recrawl_images(limit=use)
                dl = await self._prefetch_images(limit=limit or None)
                tail = ("\n继续：/buptnews images backfill <数量>" if rc["remaining"] else "")
                return (
                    "🖼 图片补齐完成\n"
                    f"重抓历史记录：{rc['updated']} 条（{rc['no_images']} 条页面无图）"
                    f"，失败 {rc['failed']} 条\n"
                    f"下载图片：成功 {dl['ok']} / 失败 {dl['failed']}"
                    f"，仍有 {dl['remaining']} 张待补\n"
                    f"剩余待补记录：{rc['remaining']} 条{tail}"
                )

            if action == "download":
                dl = await self._prefetch_images(limit=limit or None)
                return (f"🖼 图片下载：成功 {dl['ok']} / 失败 {dl['failed']}"
                        f"，仍有 {dl['remaining']} 张待补")

            if action == "retry":
                # 忽略失败上限，把账本里失败过的图再试一遍
                dl = await self._prefetch_images(limit=limit or None, force=True,
                                                 only_failed=True)
                if not dl["missing"]:
                    return "🖼 没有需要重试的失败图片"
                tail = (f"，仍有 {dl['remaining']} 张待重试" if dl["remaining"] else "")
                return (f"🖼 失败重试：成功 {dl['ok']} / 仍失败 {dl['failed']}"
                        f"（本次重试 {dl['tried']} 张{tail}）")

            files, size = self._images.stats()
            pending_records = len(records_missing_images(self._store))
            pending_images = len(missing_image_urls(self._store, self._images))
            failed = self._img_failures.count() if self._img_failures else 0
            last_err = self._img_failures.last_error() if self._img_failures else ""
            prefetch_on = "✅ 开" if conf.get("prefetch", True) else "⏸ 关"
            recrawl_on = "✅ 开" if conf.get("recrawl", True) else "⏸ 关"
            tbl_files, tbl_size = self._tables.stats() if self._tables else (0, 0)
            tbl_on = "✅ 开" if (self.config.get("tables") or {}).get(
                "enabled", True) else "⏸ 关"
            tbl_ok = "可用" if (self._tables and self._tables.available) else "不可用"
            lines = [
                "🖼 图片缓存",
                "",
                "📦 现状",
                f"  文件：{files} 个 / {size / 1048576:.1f} MB",
                f"  待下载：{pending_images} 张",
                f"  失败待重试：{failed} 张",
                f"  表格图片：{tbl_files} 个 / {tbl_size / 1048576:.1f} MB"
                f"（渲染 {tbl_on}·{tbl_ok}）",
            ]
            if last_err:
                lines.append(f"  最近错误：{last_err[:60]}")
            lines += [
                f"  待重抓历史记录：{pending_records} 条",
                "",
                "⚙️ 策略",
                f"  同步补齐：{prefetch_on}（每轮 {conf.get('prefetch_max', 30)} 张"
                f" · 失败重试 {conf.get('retry', 2)} 次）",
                f"  历史补图：{recrawl_on}（每轮 {conf.get('recrawl_max', 50)} 条）",
                "",
                "💡 用法（[数量] 可选，不填用默认值）",
                "  /buptnews images backfill [数量] — 重抓历史记录并下载",
                "  /buptnews images download [数量] — 只下载缺失的图",
                "  /buptnews images retry [数量] — 重试下载失败的图",
            ]
            return "\n".join(lines)

        @reg("buptnews.bind", permission="superuser", scope="private",
             schema=BindSchema, description="私聊可用范围：绑定群的临时会话 + 指定用户")
        async def cmd_bind(params):
            """私聊可用范围白名单（谁能私聊用小窗/私聊调命令）。

            与 `/buptnews add` 管的是**两件事**：那个是推送目标（往哪发通知），
            这个是准入（谁能私聊用命令）。混在一起会让「加一个推送用户」意外
            变成「开放私聊服务」，所以分成两条命令、两份配置。
            """
            kind = (params.kind or "user").lower()
            key = "groups" if kind == "group" else "users"
            label = "群" if kind == "group" else "用户"
            uid = int(params.target or 0)
            action = (params.action or "add").lower()
            if not uid and action != "list":
                action = "list"             # 没给号 = 只想看看现状
            ids = self._to_ids(self._binding().get(key))

            if action == "add":
                if uid in ids:
                    return f"ℹ️ {label} {uid} 已在私聊白名单（无需重复添加）"
                ids.add(uid)
                self.config.set(f"binding.{key}", sorted(ids))
                self.logger.info("[bupt_news] 私聊白名单新增 %s %s", kind, uid)
                tail = ("\n💡 该群成员可在**群聊临时会话（小窗）**用命令"
                        if kind == "group" else
                        "\n💡 该用户可直接私聊用命令")
                return (f"✅ 已把{label} {uid} 加入私聊白名单"
                        f"（当前 {len(ids)} 个）{tail}")
            if action == "del":
                if uid not in ids:
                    return (f"⚠️ {label} {uid} 不在私聊白名单"
                            f"（用 /buptnews bind {kind} list 查看）")
                ids.discard(uid)
                self.config.set(f"binding.{key}", sorted(ids))
                self.logger.info("[bupt_news] 私聊白名单移除 %s %s", kind, uid)
                return f"✅ 已把{label} {uid} 移出私聊白名单（剩余 {len(ids)} 个）"

            groups, users = sorted(self._bound_groups()), sorted(self._bound_users())
            inherited = not self._to_ids(self._binding().get("groups"))
            lines = [
                "🔐 私聊可用范围（谁能私聊用命令）",
                "",
                "👥 群聊临时会话（小窗）来源群",
                *(f"  {g}" for g in groups),
            ]
            if not groups:
                lines.append("  （无：任何群的小窗都不服务）")
            elif inherited:
                lines.append("  ↑ 未单独配置：沿用框架 bound_groups（/groups）")
            lines += ["", f"👤 直接私聊的用户（{len(users)}）"]
            lines += [f"  {u}" for u in users] or ["  （无：非超级用户不能直接私聊用命令）"]
            lines += [
                "",
                "💡 增删：/buptnews bind user|group <号> [add|del]",
                "   例：/buptnews bind group 1000000001   —— 允许该群成员的小窗",
                "       /buptnews bind user 100000004    —— 允许该用户直接私聊",
                "⚠️ 群里不受此限制；超级用户始终可用（防止把自己锁在外面）",
            ]
            return "\n".join(lines)

        @reg("buptnews.targets", permission="superuser", scope="private",
             description="查看推送目标")
        async def cmd_targets(packet):
            groups = self._int_list("groups")
            users = self._int_list("users")
            lines = ["📬 推送目标", "", f"👥 群（{len(groups)}）"]
            lines += [f"  {g}" for g in groups] or ["  （未配置）"]
            lines += ["", f"👤 用户（{len(users)}）"]
            lines += [f"  {u}" for u in users] or ["  （未配置）"]
            lines += ["", "💡 添加：/buptnews add group|user [号]",
                      "     移除：/buptnews del group|user [号]"]
            return "\n".join(lines)

        @reg("buptnews.add", permission="superuser", scope="private",
             schema=AddTargetSchema, description="添加推送目标")
        async def cmd_add(params, event):
            kind = params.kind
            target = params.target
            if not target:
                if kind == "group" and event.group_id:
                    target = int(event.group_id)
                else:
                    return "⚠️ 请指定群号/QQ号（本群可用 add group 省略）"
            target = int(target)
            key = "groups" if kind == "group" else "users"
            ids = self._int_list(key)
            if target in ids:
                return f"{'群' if kind == 'group' else '用户'} {target} 已在推送列表"
            ids.append(target)
            self.config.set(key, ids)
            self.logger.info("[bupt_news] 添加推送目标 %s %s", kind, target)
            return f"✅ 已添加推送{'群' if kind == 'group' else '用户'} {target}（当前 {len(ids)} 个）"

        @reg("buptnews.del", permission="superuser", scope="private",
             schema=DelTargetSchema, description="移除推送目标")
        async def cmd_del(params, event):
            kind = params.kind
            target = params.target
            if not target:
                if kind == "group" and event.group_id:
                    target = int(event.group_id)
                else:
                    return "⚠️ 请指定群号/QQ号"
            target = int(target)
            key = "groups" if kind == "group" else "users"
            ids = self._int_list(key)
            if target not in ids:
                return f"⚠️ {target} 不在推送列表中（用 /buptnews targets 查看）"
            ids = [x for x in ids if x != target]
            self.config.set(key, ids)
            self.logger.info("[bupt_news] 移除推送目标 %s %s", kind, target)
            return f"✅ 已移除推送{'群' if kind == 'group' else '用户'} {target}（剩余 {len(ids)} 个）"

        @reg("buptnews.on", permission="superuser", scope="private", description="开启推送")
        async def cmd_on(packet):
            self.config.set("enabled", True)
            return "✅ 推送已开启"

        @reg("buptnews.off", permission="superuser", scope="private", description="关闭推送")
        async def cmd_off(packet):
            self.config.set("enabled", False)
            return "⏸ 推送已关闭（仍同步并记录，不发送）"

        @reg("buptnews.interval", permission="superuser", scope="private",
             schema=IntervalSchema, description="设置同步间隔")
        async def cmd_interval(params):
            secs = int(params.interval)
            self.config.set("interval", secs)
            self._reschedule()
            return f"✅ 同步间隔已设为 {secs}s（{secs // 60} 分钟）"

        @reg("buptnews.sync", permission="admin", scope="all",
             schema=SyncSchema, description="立即同步一次")
        async def cmd_sync(params, event):
            pages = int(params.pages or 1)
            preview = bool(params.preview)
            temp_group = int(event.group_id) if getattr(event, "is_temp_session", False) else None
            result = await self._sync_once(
                reason="手动", pages=pages, force_push=True,
                preview_user=int(event.user_id) if preview else None,
                preview_group=temp_group,
            )
            if result is None:
                return "⚠️ 同步未完成，见日志（可能是未配置账号密码或门户不可达）"
            count, sent, failed = result
            if preview:
                return f"🔎 预览完成：新通知 {count} 条，已私聊发送给你"
            if count == 0:
                return "✅ 同步完成：没有新通知"
            return f"✅ 同步完成：新通知 {count} 条，成功推送 {sent} 个目标（失败 {failed}）"
