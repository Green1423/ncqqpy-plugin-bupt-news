"""DeepSeek（OpenAI 兼容）批量摘要客户端。

关键约束（由实测探针得出，勿轻改）：
  * `deepseek-flash` 是推理模型，reasoning 占 ~93% completion token；
    `max_tokens` 给小了会 `finish_reason=length` **且 content 为空串**（静默失败）。
  * `reasoning_effort:"low"` 被服务端忽略，不能靠它省 token。
  * 批量调用比逐条调用省约一半 token 且更稳定
    （实测：逐条 3 条 8.1s/1640 tok 且 1 条空；批量 6 条 5.9s/1551 tok 全成功）。

摘要同样是 best-effort：任何失败都返回 {}，由调用方降级为标题截断，绝不丢通知。
"""
from __future__ import annotations

import json
import re
from typing import Optional

import aiohttp

MAX_SUMMARY_CHARS = 20
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I | re.M)

SYSTEM_PROMPT = (
    "你是校园通知摘要助手。对每条通知输出不超过20个汉字的要点摘要，"
    "要求信息密度高、去掉「关于」「的通知」这类套话。"
    "同时判断该通知是否面向**学生**（本科生/研究生）：只有明确面向学生的通知"
    "（选课、考试、竞赛报名、奖助学金、学籍、宿舍、学生活动等）才算 true；"
    "面向教师、行政人员、部门、机构编制、干部任免、资产/招标等算 false。"
    "拿不准时给 true。"
    "只输出 JSON 数组，形如 "
    '[{"id":"142431","s":"摘要","student":true}]，不要任何其他文字。'
)

# 只补「是否面向学生」判定时用的提示词：标题已经在手，摘要也已经有了，
# 不需要（也不该）重新生成摘要 —— 既省 token，也避免摘要被改写。
STUDENT_ONLY_PROMPT = (
    "你是校园通知分类助手。判断下列通知是否面向**学生**（本科生/研究生）："
    "只有明确面向学生的通知（选课、考试、竞赛报名、奖助学金、学籍、宿舍、"
    "学生活动等）才算 true；面向教师、行政人员、部门、机构编制、干部任免、"
    "资产/招标等算 false。拿不准时给 true。"
    "只输出 JSON 数组，形如 "
    '[{"id":"142431","student":true}]，不要任何其他文字。'
)


class NoticeDigest:
    """单条通知的 AI 结果：摘要 + 是否面向学生。"""

    __slots__ = ("summary", "for_students")

    def __init__(self, summary: str, for_students: Optional[bool] = None):
        self.summary = summary
        self.for_students = for_students

    def __repr__(self):  # pragma: no cover - 调试用
        return (f"NoticeDigest(summary={self.summary!r}, "
                f"for_students={self.for_students!r})")


def fallback_summary(title: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """摘要不可得时的降级：标题截断。绝不返回空串。"""
    t = (title or "").strip()
    if not t:
        return "（无标题）"
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def clamp(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """压平空白并截断到 limit 字。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""
    return t if len(t) <= limit else t[:limit].rstrip()


def _err_text(e: BaseException) -> str:
    """异常的可读描述 —— **绝不抛异常**。

    两个坑都踩过：

    * 有些异常的 `str(e)` 是空串（`asyncio.TimeoutError`），只记 `{e}` 会打出
      一行"失败："后面什么都没有，完全没法排查 → 空时退回类型名；
    * `aiohttp.ClientConnectorDNSError` 在缺少 conn_key 时 `__str__` **自己会抛**
      `AttributeError: 'NoneType' object has no attribute 'ssl'` —— 于是"记录失败"
      这一步又炸一次，把整轮同步带崩。所以这里必须全程 try 住。
    """
    name = type(e).__name__
    try:
        msg = str(e).strip()
    except Exception:  # noqa: BLE001
        try:
            msg = repr(e)
        except Exception:  # noqa: BLE001
            msg = ""
    return f"{name}: {msg}" if msg else name


def _as_bool(v) -> Optional[bool]:
    """把模型给的 student 值转成 bool；认不出来返回 None。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "是", "学生"):
            return True
        if s in ("false", "no", "0", "否", "非学生"):
            return False
    return None


def parse_summaries(raw: str) -> dict[str, str]:
    """解析模型输出为 {id: 摘要}（只要摘要，忽略学生判定）。"""
    return {k: v.summary for k, v in parse_digests(raw).items()}


def parse_students(raw: str) -> dict[str, bool]:
    """解析「只问判定」的输出为 {id: bool}；认不出的条目直接丢掉。"""
    if not raw:
        return {}
    cleaned = _FENCE_RE.sub("", raw).strip()
    if not cleaned.startswith("["):
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]
    try:
        arr = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return {}
    out: dict[str, bool] = {}
    if isinstance(arr, list):
        for item in arr:
            if not isinstance(item, dict):
                continue
            key = item.get("id")
            val = _as_bool(item.get("student", item.get("for_students")))
            if key is not None and val is not None:
                out[str(key)] = val
    return out


def parse_digests(raw: str) -> dict[str, NoticeDigest]:
    """解析模型输出为 {id: NoticeDigest}；容错 ```json 围栏与前后杂字。

    `student` 字段认不出来时 `for_students=None`（调用方按「拿不准算学生」处理）。
    """
    if not raw:
        return {}
    cleaned = _FENCE_RE.sub("", raw).strip()
    if not cleaned.startswith("["):
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]
    try:
        arr = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return {}
    out: dict[str, NoticeDigest] = {}
    if isinstance(arr, list):
        for item in arr:
            if not isinstance(item, dict):
                continue
            key = item.get("id")
            val = clamp(str(item.get("s") or item.get("summary") or ""))
            if key is None or not val:
                continue
            student = _as_bool(item.get("student", item.get("for_students")))
            out[str(key)] = NoticeDigest(val, student)
    return out


class Summarizer:
    """批量摘要。任何失败都返回 {}，由调用方降级。"""

    def __init__(self, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-flash", api_key: str = "",
                 batch_size: int = 10, max_tokens: int = 4096,
                 timeout: int = 60, reasoning_effort: str = "none",
                 logger=None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.batch_size = max(1, int(batch_size))
        self.max_tokens = max(256, int(max_tokens))
        self.timeout = timeout
        # 摘要很短，思考链纯属浪费。实测同一个 prompt：
        #   reasoning_effort="low"  → reasoning 454 / completion 466（只是减少，没关掉）
        #   reasoning_effort="none" → reasoning   0 / completion  10（真正关闭）
        #   enable_thinking=false / chat_template_kwargs → 无效，均仍 ~465
        # "none" 不在服务端公布的 low/high/max 列表里，但实测被接受。
        self.reasoning_effort = reasoning_effort
        self.logger = logger
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_error: Optional[str] = None   # 最近一次批次失败原因（便于诊断）

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    async def _ensure_session(self, timeout: int | float | None = None
                              ) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout or self.timeout,
                                              connect=10))
        return self._session

    async def _one_batch(self, items: list[tuple[str, str]], *,
                         system: str = SYSTEM_PROMPT,
                         ask: str = "请总结以下通知：",
                         parse=parse_digests,
                         timeout: int | float | None = None) -> dict:
        """一次批量请求。

        ⚠️ `parse` 必须与 `system` 配套：只问判定的回复里**没有 `s` 字段**，
        用 `parse_digests` 解析会被整条丢掉（表现为"0 条成功、且没有任何报错"）。
        """
        listing = "\n".join(f"{nid}. {title}" for nid, title in items)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"{ask}\n{listing}"},
            ],
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        effort = str(self.reasoning_effort or "").strip().lower()
        if effort and effort not in ("off", "inherit", "default"):
            payload["reasoning_effort"] = effort
        session = await self._ensure_session(timeout)
        async with session.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                raise RuntimeError(f"HTTP {resp.status}: {body}")
            data = await resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if not content.strip():
            # 推理模型在 max_tokens 不足时会返回空 content —— 必须显式报错，
            # 否则会静默推送空摘要。
            raise RuntimeError(
                "空响应（finish_reason=%s）；请调大 summarize.max_tokens"
                % choice.get("finish_reason"))
        parsed = parse(content)
        if not parsed:
            # 有内容却解析不出任何条目 → 明确报错。
            # 否则调用方只看到"0 条成功、没有任何异常"，无从排查
            # （曾因「只问判定」的回复缺 `s` 字段被 parse_digests 整条丢掉）。
            raise RuntimeError(
                f"响应无法解析为预期 JSON（finish_reason={choice.get('finish_reason')}，"
                f"前 120 字：{content[:120]!r}）")
        return parsed

    async def summarize_students(self, notices, *, timeout: int | float | None = None,
                                 batch_size: int | None = None) -> dict[str, bool]:
        """**只**补「是否面向学生」的判定，不重新生成摘要。

        用于「摘要已有、但缺学生判定」的记录：标题就够了，省掉重新摘要的 token，
        也避免把已经发出去的摘要改写成另一个版本。返回 `{newsid: bool}`。
        """
        if not self.enabled or not notices:
            return {}

        def pair(n):
            if isinstance(n, dict):
                return str(n.get("newsid", "")), str(n.get("title", ""))
            return str(n.newsid), n.title

        out: dict[str, bool] = {}
        items = list(notices)
        step = max(1, int(batch_size or self.batch_size))
        for i in range(0, len(items), step):
            chunk = items[i:i + step]
            pairs = [pair(n) for n in chunk]
            pairs = [(a, b) for a, b in pairs if a]
            if not pairs:
                continue
            try:
                digests = await self._one_batch(
                    pairs, system=STUDENT_ONLY_PROMPT,
                    ask="请判断以下通知是否面向学生：", parse=parse_students,
                    timeout=timeout)
            except Exception as e:  # noqa: BLE001 - best-effort
                self.last_error = _err_text(e)
                if self.logger:
                    self.logger.warning("[bupt_news] 学生判定失败（%d 条）：%s",
                                        len(pairs), _err_text(e))
                continue
            out.update(digests)
        return out

    async def summarize(self, notices) -> dict[str, NoticeDigest]:
        """notices 可以是对象列表（具 .newsid/.title）或记录 dict 列表。

        返回 `{newsid: NoticeDigest}`（摘要 + 是否面向学生）。
        """
        if not self.enabled or not notices:
            return {}

        def pair(n):
            if isinstance(n, dict):
                return str(n.get("newsid", "")), str(n.get("title", ""))
            return str(n.newsid), n.title

        out: dict[str, NoticeDigest] = {}
        items = list(notices)
        for i in range(0, len(items), self.batch_size):
            chunk = items[i:i + self.batch_size]
            pairs = [pair(n) for n in chunk]
            pairs = [(a, b) for a, b in pairs if a]
            if not pairs:
                continue
            try:
                out.update(await self._one_batch(pairs))
            except Exception as e:  # noqa: BLE001 - 摘要 best-effort
                # 记下来：否则调用方只看到「0 条成功」，不知道是 400 模型名错、
                # 429 限流还是 500 上游故障。
                self.last_error = _err_text(e)
                if self.logger:
                    self.logger.warning("[bupt_news] 摘要失败（%d 条将降级）：%s",
                                        len(pairs), _err_text(e))
        return out

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
