"""北邮「第二课堂」客户端 —— 基于 JWT 的活动信息拉取。

协议见 `afterclass/API.md`（逆向小程序产物得到）。与门户（`portal.py`）不同，
这里的鉴权是 `Authorization: Bearer <JWT>`，**没有** 可复现的账号密码登录路径：
`POST /api/v1/auth/sessions` 在需要人机校验时返回 419/420，而验证码页面
`/mp-captcha.html` 是商业反爬（瑞数 `$_ts` 混淆）。绕过它属于规避访问控制，
所以本模块**只接受用户自己提供的 token**（小程序 Storage `tokenv3` 或 H5 地址栏
`#token=`），不实现登录。

token 来源优先级：显式参数 > 环境变量 `DEKT_TOKEN` > 文件 `afterclass/token.txt`。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any, Optional

import aiohttp

DEKT_BASE = "https://dekt.bupt.edu.cn"
API_PREFIX = "/api/v1"
#: ⚠️ **锁死成微信客户端 UA**：服务端只认 `MicroMessenger` 客户端的指纹，
#: 换别的（桌面 Chrome、纯 curl、或"Linux + MicroMessenger"这种自相矛盾的组合）
#: 会被 WAF / 风控挡掉。所以这里固定成一台 iPhone 上微信小程序的 UA，
#: **不要按平台改、也不要随机轮换** —— 换一次就等于换一台设备，反而更可疑。
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.50(0x1800323c) NetType/WIFI Language/zh_CN miniProgram"
)
# 小程序会把版本号一起带上；服务端对缺失它多数接口仍放行，带上更接近真实客户端
UAPP_VERSION = "1.0.0"
#: 小程序 GET 的重试策略（API.md §1）：非 2xx / 网络错误最多重试 2 次，
#: 每次追加 `_fly_retry=n`、超时 +2s（首次 2s → 4s → 6s）。
GET_RETRIES = 2
GET_TIMEOUT = 2.0
GET_TIMEOUT_STEP = 2.0
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "afterclass", "token.txt")


def _retryable(err: Exception) -> bool:
    """这个错误值不值得重试：5xx 与 429 算，其余 4xx 不算（再试也一样）。"""
    status = int(getattr(err, "status", 0) or 0)
    return status >= 500 or status == 429


def _device_file(token_file: str) -> str:
    """设备号落盘路径：跟 token 放一起（同一"安装"应保持同一设备号）。"""
    return os.path.join(os.path.dirname(os.path.abspath(token_file)) or ".",
                        "device36.txt")


def _load_or_make_device36(token_file: str) -> str:
    """读/建设备号（base36，7~12 位）。

    为什么必须稳定：小程序每个鉴权请求都带 `_=<device36><ts>`，服务端把它当
    设备指纹的一部分；每次重启换一个号比固定一个号更像爬虫。
    """
    path = _device_file(token_file)
    try:
        with open(path, encoding="utf-8") as f:
            got = f.read().strip()
        if got and all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in got):
            return got
    except OSError:
        pass
    raw = int.from_bytes(os.urandom(6), "big")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while raw:
        raw, rem = divmod(raw, 36)
        out.append(digits[rem])
    value = "".join(reversed(out)) or "0"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # 与 token 同目录，权限口径统一（不是凭据，但别留个写权限给别人）
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
    except OSError:
        pass                                 # 落不了盘就退化成本进程内固定
    return value


class DektError(RuntimeError):
    """第二课堂接口调用失败。`status` 是 HTTP 状态（拿不到就是 0）。"""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = int(status or 0)


class DektAuthError(DektError):
    """token 缺失/过期/无效（401）。"""


def decode_jwt(token: str) -> dict:
    """解出 JWT 的 payload（不校验签名 —— 只用于取 `sub`/`exp`）。

    JWT 的 payload 是 base64url 编码的 JSON，这里补齐 padding 后解码。
    解析失败返回空 dict（调用方据此报"token 不合法"）。
    """
    parts = str(token or "").strip().split(".")
    if len(parts) < 2:
        return {}
    body = parts[1].replace("-", "+").replace("_", "/")
    body += "=" * (-len(body) % 4)
    try:
        data = json.loads(base64.b64decode(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def clean_token(value) -> str:
    """把用户粘贴的 token 归一化。

    从浏览器控制台复制会带引号（`"eyJ…"`），从设置页复制可能带 `Bearer ` 前缀，
    有时还会混进换行 —— 这些都很常见，直接当 token 用只会得到 401。
    """
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    # 多行粘贴时只取第一段像 JWT 的内容
    for line in text.splitlines():
        line = line.strip().strip("'\"").strip()
        if line.count(".") >= 2:
            return line
    return text.replace("\n", "").strip()


def load_token(explicit: str = "", path: str = TOKEN_FILE) -> str:
    """按 参数 > 环境变量 > 文件 的顺序找 token（都过一遍 clean_token）。"""
    for candidate in (explicit, os.environ.get("DEKT_TOKEN", "")):
        if candidate and str(candidate).strip():
            return clean_token(candidate)
    try:
        with open(path, encoding="utf-8") as f:
            return clean_token(f.read())
    except OSError:
        return ""


def _unwrap(payload: Any, status: int) -> dict:
    """把 `{data, digest, msg, code}` 统一成 dict；错误转成异常。"""
    if status == 401:
        raise DektAuthError("token 无效或已过期")
    if not isinstance(payload, dict):
        raise DektError(f"响应不是 JSON 对象（HTTP {status}）")
    if status >= 400 or payload.get("status") == "error":
        msg = payload.get("error") or payload.get("message") or payload.get("msg") \
            or f"HTTP {status}"
        raise DektError(str(msg), status=status)
    return payload


class DektClient:
    """第二课堂接口客户端（只读部分：用户、活动、分类）。"""

    def __init__(self, token: str = "", *, base: str = DEKT_BASE,
                 timeout: float = 20.0, logger=None, token_file: str = TOKEN_FILE):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.logger = logger
        self._token_file = token_file
        self.token = load_token(token, token_file)
        self._session: Optional[aiohttp.ClientSession] = None
        #: 设备号（base36）：小程序的 `_` 参数是 `<device36><毫秒时间戳>`，
        #: 同一台"设备"要稳定 —— 每次随机反而更像爬虫。落在 token 同目录。
        self._device36 = _load_or_make_device36(token_file)
        #: 会话 cookie（小程序存 Storage 的 `cookie._rab` / `cookie._vab`）
        self._cookies: dict[str, str] = {}

    # ---------------- 会话 ----------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "X-UApp-Version": UAPP_VERSION,
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=10),
                headers=headers,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def user_id(self) -> str:
        """JWT 的 `sub`（接口路径里的 `{currentUserId}`）。"""
        sub = decode_jwt(self.token).get("sub")
        return str(sub) if sub is not None else ""

    @property
    def available(self) -> bool:
        return bool(self.token and self.user_id)

    def reload_token(self) -> bool:
        """重新从文件/环境变量读 token，返回**是否发生变化**。

        这样续期只要覆盖 `token.txt` 即可，不用重启插件 —— 手动那一步
        （过验证码登录、复制 token）无法自动化，至少别让它还要重启服务。
        """
        fresh = load_token("", self._token_file)
        if fresh == self.token:
            return False
        self.token = fresh
        # 会话头里带着 Authorization，token 变了必须重建 session
        if self._session is not None and not self._session.closed:
            asyncio.create_task(self._session.close())
        self._session = None
        return True

    def expiry(self):
        """token 的过期时间（`datetime`）；取不到返回 None。"""
        exp = decode_jwt(self.token).get("exp")
        if not isinstance(exp, (int, float)):
            return None
        from datetime import datetime
        try:
            return datetime.fromtimestamp(float(exp))
        except (OverflowError, OSError, ValueError):
            return None

    # ---------------- 基础请求 ----------------

    def _common_query(self) -> dict:
        """小程序的公共查询参数：`_=<device36><毫秒时间戳>`（API.md §1）。"""
        return {"_": f"{self._device36}{int(time.time() * 1000)}"}

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET，**行为对齐小程序**：公共 `_` 参数、cookie 回传、失败重试。

        重试口径（API.md §1）：非 2xx 或网络错误最多重试 2 次，每次追加
        `_fly_retry=n` 并把超时 +2s（2s → 4s → 6s）。401 不重试 —— 那是
        token 失效，再试一百次也一样。
        """
        if not self.token:
            raise DektAuthError(
                "缺少 token：把它放进 afterclass/token.txt、环境变量 DEKT_TOKEN，"
                "或调用时传入")
        url = f"{self.base}{path}"
        last_error: Optional[Exception] = None
        for attempt in range(GET_RETRIES + 1):
            query = self._common_query()
            query.update({k: v for k, v in (params or {}).items() if v != ""})
            if attempt:
                query["_fly_retry"] = str(attempt)
            timeout = aiohttp.ClientTimeout(
                total=GET_TIMEOUT + GET_TIMEOUT_STEP * attempt,
                connect=GET_TIMEOUT)
            session = await self._ensure_session()
            try:
                async with session.get(url, params=query, cookies=self._cookies,
                                       timeout=timeout) as resp:
                    self._remember_cookies(resp)
                    text = await resp.text()
                    real = resp.headers.get("x-real-status") or resp.status
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        payload = None
                    return _unwrap(
                        payload,
                        int(real) if str(real).isdigit() else resp.status)
            except DektAuthError:
                raise                        # 401 不重试
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < GET_RETRIES:
                    if self.logger:
                        self.logger.info(
                            "[bupt_news] 第二课堂 GET 失败（%s），第 %d/%d 次重试",
                            type(e).__name__, attempt + 1, GET_RETRIES)
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                raise DektError(f"{type(e).__name__}: {e}") from e
            except DektError as e:
                # 服务端 5xx：与网络错误同样处理（重试），4xx 直接抛
                last_error = e
                if attempt < GET_RETRIES and _retryable(e):
                    continue
                raise
        raise DektError(str(last_error or "请求失败"))

    def _remember_cookies(self, resp) -> None:
        """记下小程序会存 Storage 的那两个 cookie（`_rab` / `_vab`）。"""
        try:
            for key in ("_rab", "_vab"):
                value = resp.cookies.get(key)
                if value is not None:
                    self._cookies[key] = value.value
        except Exception:                    # noqa: BLE001 - cookie 不该影响主流程
            pass

    # ---------------- 业务接口 ----------------

    async def me(self) -> dict:
        """当前用户资料 + `digest`（活动列表要用它）。"""
        uid = self.user_id
        if not uid:
            raise DektAuthError("token 里没有 sub，无法定位用户")
        got = await self._get(f"{API_PREFIX}/user/{uid}")
        data = got.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        return {"user": data, "digest": got.get("digest")}

    async def activity_classes(self) -> list[dict]:
        """活动分类（`id → name`，展示时把 `class_id` 翻成名字）。"""
        got = await self._get(f"{API_PREFIX}/activity/class")
        data = got.get("data") or []
        return [d for d in data if isinstance(d, dict)]

    async def activities(self, *, digest: Any = "", college_id: Any = 0,
                         class_id: Any = 0, grade: Any = 0, role_id: Any = 0,
                         is_group: int = 0) -> list[dict]:
        """活动列表。返回 `[{activity..., "digest": <服务端摘要>}]`。

        `digest` 用 `me()` 返回的用户摘要；服务端把活动数组和等长的 digest
        数组分开给（digest 用于后续报名等写操作校验），这里按下标配回每条活动。
        """
        got = await self._get(f"{API_PREFIX}/activity", params={
            "college_id": college_id, "class_id": class_id, "grade": grade,
            "role_id": role_id, "digest": digest, "is_group": is_group,
        })
        data = got.get("data") or []
        if isinstance(data, dict):        # 防御：有的接口把列表包一层
            data = data.get("list") or data.get("data") or []
        digests = got.get("digest") or []
        if not isinstance(digests, list):
            digests = []
        out: list[dict] = []
        for idx, item in enumerate(data if isinstance(data, list) else []):
            if not isinstance(item, dict):
                continue
            rec = dict(item)
            rec["digest"] = digests[idx] if idx < len(digests) else None
            out.append(rec)
        return out

    async def fetch_activities(self, user: dict, *, digest: Any = "",
                               classes: Optional[dict] = None) -> list[dict]:
        """**按官方小程序的顺序**把活动扒下来（用户要求"模仿官方 app 的行为"）。

        小程序的初始化顺序（final_draft §4.2 / API.md §5）：

        1. `activity/class` 拿分类（全局状态里缓存）；
        2. `activity?is_group=0` 拿**普通活动**；
        3. `activity?is_group=1` 拿**组织活动**（页面上切到对应分类时按需加载，
           这里一次拉齐 —— 否则同步会漏掉组织活动）；
        4. 两份合并、按 `id` 去重、再按小程序的默认排序输出。

        参数口径与小程序一致：`college_id` / `class_id` / `grade` / `role_id`
        取自 `GET /user/{sub}`，`digest` 用用户摘要（不带可能被拒）。
        """
        if classes is None:
            classes = {str(c.get("id")): str(c.get("name") or "")
                       for c in await self.activity_classes()}
        #: 分类缓存（小程序也在全局状态里缓存 `activityClass`）
        self.classes = dict(classes)
        common = {
            "digest": digest or "",
            "college_id": user.get("college_id") or 0,
            "class_id": user.get("class_id") or 0,
            "grade": user.get("grade") or 0,
            "role_id": user.get("role_id") or 0,
        }
        normal = await self.activities(**common, is_group=0)
        group: list[dict] = []
        try:
            group = await self.activities(**common, is_group=1)
        except DektError as e:
            # 组织活动是"额外一份"，拉不到不该让整轮失败（小程序也是按需加载）
            if self.logger:
                self.logger.info("[bupt_news] 组织活动拉取失败（不影响普通活动）：%s", e)

        merged: dict[str, dict] = {}
        for is_group, acts in ((0, normal), (1, group)):
            for act in acts:
                key = activity_key(act)
                if key in merged:
                    continue
                rec = dict(act)
                rec["is_group"] = is_group
                merged[key] = rec
        if self.logger:
            self.logger.info(
                "[bupt_news] 活动拉取：普通 %d 条 / 组织 %d 条 → 去重后 %d 条",
                len(normal), len(group), len(merged))
        return app_sort(merged.values())


    async def activity(self, act_id: Any) -> dict:
        """活动详情。"""
        got = await self._get(f"{API_PREFIX}/activity/{int(act_id)}")
        data = got.get("data") or {}
        return data if isinstance(data, dict) else {}


def app_sort(acts) -> list[dict]:
    """官方小程序的默认排序（final_draft §4.2）。

    报告只说了"结合活动优先级、开始时间、报名窗口和本地计算权重"，没有给权重
    公式。这里用一条**可解释**的近似，顺序容易复盘：

    1. `priority` 大的在前（服务端本身就是按它排的）；
    2. 报名窗口**正开着**的在前（"现在能报"比"还没开"更急）；
    3. 开始时间早的在前（同样的优先级，先发生的先看）；
    4. 最后用 `id` 兜底，保证顺序稳定。

    ⚠️ 这是近似，不是复刻；真实权重在客户端 `app-service.js` 里，报告未给。
    """
    now = time.time()

    def _ts(value) -> float:
        if not value:
            return 0.0
        try:
            text = str(value).replace("Z", "+00:00")
            from datetime import datetime
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0

    def key(act: dict):
        priority = int(act.get("priority") or 0)
        open_now = 1
        start = _ts(act.get("signup_start_time"))
        end = _ts(act.get("signup_end_time"))
        if start and end and not (start <= now <= end):
            open_now = 0
        return (-priority, -open_now, _ts(act.get("activity_start_time")) or 9e18,
                str(activity_key(act)))

    return sorted(list(acts), key=key)


def activity_key(act: dict) -> str:
    """活动的稳定标识（用 `id`；缺失时退回标题+时间）。"""
    for field in ("id", "act_id", "activity_id"):
        if act.get(field) is not None:
            return str(act[field])
    return f"{act.get('title') or act.get('name') or ''}@{act.get('start_t_ms')}"


# ---------------- 活动归一化 ----------------
#
# ⚠️ 这些字段名来自 API 文档的**枚举与用途**，没有真实响应样本可核对，
# 所以统一走"别名表 + 容错转换"：命中哪个别名都行，全都取不到就留空。
# 第一次真实拉取后，看 `normalize_activity()` 的输出把别名表收紧即可。

ACTIVITY_AREAS = {0: "西土城", 1: "沙河", 2: "线上", 3: "海南", 100000000: "其他"}

CUSTOMER_LEVELS = {
    0: "全校", 1: "全校按年级", 2: "学院", 3: "学院按年级",
    4: "班级", 5: "社团", 6: "自定义",
}

DEMANDS = {"apply": 1, "checkin": 2, "checkout": 4, "channel": 8,
           "admin": 16, "hidden": 32, "role_filter": 64}

# ⚠️ 这些名字是**按真实响应核对过**的（`tools/dekt_probe.py --json` 会打印字段名），
# 不是猜的。门户改字段时用同一个探针复查。
_ALIASES = {
    "id": ("id", "act_id", "activity_id"),
    "title": ("name", "title", "act_name", "topic", "subject"),
    "place": ("location", "place", "address", "act_place", "site", "room"),
    "area": ("area", "act_area", "activity_area"),
    "class_id": ("activity_class", "class_id", "act_class_id",
                 "activity_class_id", "type_id"),
    "attend_limit": ("attend_limit", "limit", "quota", "max_attend"),
    "class_name": ("class_name", "act_class_name", "activity_class_name", "type_name"),
    "customer": ("customer", "customer_level", "level"),
    "demands": ("demands", "demand", "demand_flag"),
    # 活动本身的时间（真实字段是 ISO8601 UTC 字符串，如 2026-10-26T07:00:00Z）
    "start_t_ms": ("activity_start_time", "start_t_ms", "act_start_t_ms",
                   "start_time", "start_at", "start"),
    "end_t_ms": ("activity_end_time", "end_t_ms", "act_end_t_ms",
                 "end_time", "end_at", "end"),
    # 报名窗口
    "apply_start_t_ms": ("signup_start_time", "apply_start_t_ms", "enroll_start_t_ms",
                         "apply_start_time", "join_start_t_ms"),
    "apply_end_t_ms": ("signup_end_time", "apply_end_t_ms", "enroll_end_t_ms",
                       "apply_end_time", "join_end_t_ms"),
    "org_name": ("org_name", "group_name", "college_name", "publisher", "sponsor"),
    "summary": ("summary", "digest_text", "description", "intro", "content_text"),
}


def _pick(act: dict, field: str):
    """按别名表取值（空串/None 跳过）。"""
    for name in _ALIASES.get(field, ()):
        if name in act and act[name] not in (None, "", []):
            return act[name]
    return None


# 服务端用 1970-01-01 表示"没有这个时间"（实测 `signup_*_time` 常见），
# 直接显示会变成"报名 1970-01-01 08:00"，比不显示更糟。
_NO_TIME_YEAR = 2000


def to_local_time(value) -> str:
    """把时间值转成 `YYYY-MM-DD HH:MM`；哨兵值（1970 等）返回空串。

    可能是 ISO8601（`2026-10-26T07:00:00Z`，真实格式）、毫秒/秒时间戳、
    `2026-10-08 12:00:00` 这类字符串；认不出就原样返回。
    """
    if value in (None, "", 0):
        return ""
    text = str(value).strip()
    # ISO8601（`2026-10-26T07:00:00Z` / 带 +08:00 偏移）：转成本机时区再格式化
    if "T" in text or (len(text) >= 19 and text[10] in " T"):
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone()          # 转到本机时区（这台机器是 CST/UTC+8）
            if dt.year < _NO_TIME_YEAR:       # 1970-01-01 = "无此时间"的哨兵
                return ""
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    if text.isdigit():
        num = int(text)
        # 秒级时间戳（10 位）与毫秒级（13 位）都接受
        if num > 10 ** 12:
            num /= 1000.0
        elif num < 10 ** 9:
            return text                     # 明显不是时间戳，原样返回
        try:
            from datetime import datetime
            dt = datetime.fromtimestamp(num)
            if dt.year < _NO_TIME_YEAR:
                return ""
            return dt.strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            return text
    return text


def normalize_activity(act: dict, classes: Optional[dict] = None) -> dict:
    """把一条原始活动压成展示/入库用的字段（全部容错，缺就不填）。"""
    act = act if isinstance(act, dict) else {}
    classes = classes or {}
    class_id = _pick(act, "class_id")
    class_name = _pick(act, "class_name") or classes.get(str(class_id)) or ""
    area = _pick(act, "area")
    demands = _pick(act, "demands")
    try:
        demands = int(demands)
    except (TypeError, ValueError):
        demands = 0
    return {
        "id": activity_key(act),
        "title": str(_pick(act, "title") or "").strip(),
        "place": str(_pick(act, "place") or "").strip(),
        "area": ACTIVITY_AREAS.get(area, "") if area is not None else "",
        "class_id": class_id,
        "class_name": str(class_name or ""),
        "org_name": str(_pick(act, "org_name") or "").strip(),
        "customer": CUSTOMER_LEVELS.get(_pick(act, "customer"), ""),
        "demands": demands,
        "can_apply": bool(demands & DEMANDS["apply"]),
        "hidden": bool(demands & DEMANDS["hidden"]),
        "start": to_local_time(_pick(act, "start_t_ms")),
        "end": to_local_time(_pick(act, "end_t_ms")),
        "apply_start": to_local_time(_pick(act, "apply_start_t_ms")),
        "apply_end": to_local_time(_pick(act, "apply_end_t_ms")),
        "attend_limit": _pick(act, "attend_limit"),
        "summary": str(_pick(act, "summary") or "").strip(),
        "raw": act,
    }


def format_activity(norm: dict, *, base: str = DEKT_BASE) -> str:
    """把归一化后的活动渲染成两三行推送文案。"""
    title = norm.get("title") or "（无标题活动）"
    # 标题用 ▸ 而不是 🎪：批量推送时 🎪 已经用在消息头，每条再来一个太吵
    lines = [f"▸ {title}"]
    meta = " · ".join(x for x in (norm.get("org_name"), norm.get("class_name"),
                                  norm.get("area")) if x)
    if meta:
        lines.append(f"🏛 {meta}")
    when = ""
    if norm.get("start"):
        when = norm["start"] + (f" ~ {norm['end'][11:]}" if norm.get("end") else "")
    elif norm.get("apply_end"):
        when = f"报名截止 {norm['apply_end']}"
    if when:
        lines.append(f"🕐 {when}")
    place = norm.get("place") or ""
    limit = norm.get("attend_limit")
    if place or limit:
        tail = f"（限 {limit} 人）" if limit else ""
        lines.append(f"📍 {place}{tail}".rstrip())
    apply_line = ""
    if norm.get("apply_start") and norm.get("apply_end"):
        apply_line = f"报名 {norm['apply_start']} ~ {norm['apply_end']}"
    elif norm.get("apply_end"):
        apply_line = f"报名截止 {norm['apply_end']}"
    if apply_line:
        lines.append(f"📝 {apply_line}")
    if norm.get("id"):
        lines.append(f"🔗 {base}/activity/{norm['id']}")
    return "\n".join(lines)
