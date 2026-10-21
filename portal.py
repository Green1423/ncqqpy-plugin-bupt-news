"""BUPT 信息服务门户客户端 —— 统一身份认证 (Apereo CAS) + 校内通知抓取。

依赖：标准库 + aiohttp（项目已声明依赖）。
站点为 Visual SiteBuilder 9，页面结构见 README 中的说明。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp

PORTAL = "http://my.bupt.edu.cn"
CAS_MARK = "authserver/login"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class PortalError(RuntimeError):
    """门户访问失败。"""


class PortalAuthError(PortalError):
    """统一身份认证失败（凭据错误 / 触发验证码 / 重登失败）。"""


@dataclass
class Notice:
    """一条通知。"""

    column: str
    newsid: str
    treeid: str
    title: str
    dept: str = ""
    date: str = ""
    url: str = ""
    body: str = ""
    id: int = 0          # 落库后由 StateStore 分配的短编号（供 /buptnews get 查询）
    # 正文素材，按原文顺序混排：图片 {"pos", "url"}，表格 {"pos", "table"}。
    # `pos` 是"该素材之前已产出的正文字符数"，两者共用同一套偏移语义。
    images: list = field(default_factory=list)
    # 表格原始 HTML（`images` 里的 "table" 是它的下标）。
    # 表必须存原文——正文文本已被拍平，丢了 HTML 就再也渲染不出行列关系。
    tables: list = field(default_factory=list)

    @property
    def key(self) -> str:
        """去重键：同一通知可能在多个栏目出现，用 treeid:newsid 唯一标识。"""
        return f"{self.treeid}:{self.newsid}"

    def as_dict(self) -> dict:
        return {
            "column": self.column,
            "newsid": self.newsid,
            "treeid": self.treeid,
            "title": self.title,
            "dept": self.dept,
            "date": self.date,
            "url": self.url,
            "images": list(self.images),
            "tables": list(self.tables),
        }


# ------------------------------------------------------------------ HTML 解析

_TAG_RE = re.compile(r"<[^>]+>")
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
_A_RE = re.compile(r"<a\b([^>]*wbnewsid[^>]*)>(.*?)</a>", re.S | re.I)
_HREF_RE = re.compile(r'href\s*=\s*"([^"]*)"', re.I)
_TITLE_RE = re.compile(r'title\s*=\s*"([^"]*)"', re.I)
_AUTHOR_RE = re.compile(
    r'<span\b[^>]*class\s*=\s*"[^"]*\bauthor\b[^"]*"[^>]*>(.*?)</span>', re.S | re.I
)
_TIME_RE = re.compile(
    r'<span\b[^>]*class\s*=\s*"[^"]*\btime\b[^"]*"[^>]*>(.*?)</span>', re.S | re.I
)
_TOTALPAGE_RE = re.compile(r"totalpage=(\d+)")
_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.S | re.I)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)
_DEPT_RE = re.compile(r"发布部门[：:]\s*([^\s：:]{1,40})")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# 正文里的图片：抽取时先埋一个 sentinel，规范化文本后再切开，
# 这样既能拿到纯文本 body，又能拿到每张图在 body 中的字符偏移（用于还原排版）。
_IMG_MARK = "\x01IMG:"
_IMG_END = "\x01"
_IMG_SENTINEL_RE = re.compile(re.escape(_IMG_MARK) + r"([^\x01]*)" + re.escape(_IMG_END))

# 表格哨兵：正文里的表格位置。与图片哨兵同一套 `\x01` 分隔约定，
# 但**不**复用图片那个（表格要保留原始 HTML，见 table_image.py）。
_TBL_MARK = "\x01TBL:"
_TBL_END = "\x01"
_TABLE_SENTINEL_RE = re.compile(
    re.escape(_TBL_MARK) + r"(\d+)" + re.escape(_TBL_END))
_IMG_SRC_ATTRS = ("src", "data-src", "data-original", "data-url", "_src")


def _text(html: str) -> str:
    """去标签取纯文本。"""
    s = _TAG_RE.sub("", html or "")
    s = unescape(s)
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _pick_img_src(attrs: dict) -> str:
    """从 <img> 属性里挑出真实图片地址（含常见懒加载写法）。"""
    for key in _IMG_SRC_ATTRS:
        v = (attrs.get(key) or "").strip()
        if v and not v.startswith(("javascript:", "#", "about:")):
            return v
    srcset = (attrs.get("srcset") or "").strip()
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0].strip()
        if first and not first.startswith(("javascript:", "#", "about:")):
            return first
    return ""


def _tidy_chunk(s: str) -> str:
    """sentinel 被切开后，补齐原本会被全局规范化吃掉的行首行尾空格。"""
    return re.sub(r" *\n *", "\n", s)


def _input_value(html: str, name: str) -> Optional[str]:
    """取 <input name="..."> 的 value。"""
    m = re.search(
        r'<input\b[^>]*name\s*=\s*"%s"[^>]*>' % re.escape(name), html or "", re.I
    )
    if not m:
        return None
    v = re.search(r'value\s*=\s*"([^"]*)"', m.group(0), re.I)
    return unescape(v.group(1)) if v else None


def parse_total_pages(html: str) -> int:
    vals = [int(v) for v in _TOTALPAGE_RE.findall(html or "")]
    return max(vals) if vals else 1


def parse_list(html: str, column, base: str = PORTAL) -> list[Notice]:
    """解析 list.jsp 的 <li> 列表项。"""
    notices: list[Notice] = []
    seen: set[str] = set()
    for block in _LI_RE.findall(html or ""):
        m = _A_RE.search(block)
        if not m:
            continue
        attrs, inner = m.group(1), m.group(2)
        hm = _HREF_RE.search(attrs)
        if not hm:
            continue
        href = unescape(hm.group(1))
        q = parse_qs(urlparse(href).query)
        newsid = (q.get("wbnewsid") or [""])[0]
        treeid = (q.get("wbtreeid") or [""])[0]
        if not newsid:
            continue
        key = f"{treeid}:{newsid}"
        if key in seen:
            continue
        seen.add(key)

        tm = _TITLE_RE.search(attrs)
        title = unescape(tm.group(1)).strip() if tm else _text(inner)
        if not title:
            continue
        am = _AUTHOR_RE.search(block)
        dm = _TIME_RE.search(block)
        notices.append(
            Notice(
                column=str(column),
                newsid=newsid,
                treeid=treeid,
                title=title,
                dept=_text(am.group(1)) if am else "",
                date=_text(dm.group(1)) if dm else "",
                url=urljoin(base + "/", href),
            )
        )
    return notices


# 门户检索结果页：每条是 `<tr><li>链接+标题+点击数</li><td>日期</td></tr>`，
# 日期在 <li> **外面**，所以不能直接复用 parse_list（那里按 <li> 取时间）。
_SEARCH_ROW_RE = re.compile(r"<tr\b[^>]*>(?:(?!</tr>).)*?</tr>", re.S | re.I)
_SEARCH_TOTAL_RE = re.compile(r"共\s*(\d+)\s*条")
_SEARCH_PAGE_PARAM = "a218493p"      # Word 风格的私有分页参数（页面自报）
_SEARCH_TOTAL_PARAM = "a218493t"
_SEARCH_PAGE_SIZE_PARAM = "a218493c"


def parse_search_total(html: str) -> int:
    """检索结果页的「共 N 条」；取不到返回 0。"""
    m = _SEARCH_TOTAL_RE.search(html or "")
    try:
        return int(m.group(1)) if m else 0
    except (TypeError, ValueError):
        return 0


def parse_search_results(html: str, base: str = PORTAL) -> list[Notice]:
    """解析 fz_ssjg.jsp 的检索结果。

    与 `parse_list` 的差别：日期是同一 `<tr>` 里独立的 `<td>`，不在 `<li>` 内；
    链接也可能是**绝对地址**（门户会把同一通知挂到 mymob 等子站）。
    """
    notices: list[Notice] = []
    seen: set[str] = set()
    for row in _SEARCH_ROW_RE.findall(html or ""):
        m = _A_RE.search(row)
        if not m:
            continue
        attrs, inner = m.group(1), m.group(2)
        hm = _HREF_RE.search(attrs)
        if not hm:
            continue
        href = unescape(hm.group(1))
        q = parse_qs(urlparse(href).query)
        newsid = (q.get("wbnewsid") or [""])[0]
        treeid = (q.get("wbtreeid") or [""])[0]
        if not newsid:
            continue
        key = f"{treeid}:{newsid}"
        if key in seen:
            continue
        seen.add(key)

        tm = _TITLE_RE.search(attrs)
        title = unescape(tm.group(1)).strip() if tm else _text(inner)
        if not title:
            continue
        # 日期取该行里最后一个 YYYY-MM-DD（点击数之类的数字不会误命中）
        dates = re.findall(r"20\d{2}-\d{2}-\d{2}", row)
        notices.append(Notice(
            column=treeid or "",
            newsid=newsid,
            treeid=treeid,
            title=title,
            date=dates[-1] if dates else "",
            url=urljoin(base + "/", href),
        ))
    return notices


def _search_form(html: str, column) -> dict:
    """从栏目列表页里抄出检索表单的 `wbtreeids`（栏目树 id 列表）。

    写死树 id 会在门户调整栏目时静默漏结果，所以每次从页面读。
    找不到时返回空串 —— 服务端会退回"全站检索"。
    """
    m = re.search(
        r'<form[^>]+action="fz_ssjg\.jsp[^"]*"[^>]*>(.*?)</form>',
        html or "", re.S | re.I)
    if not m:
        return {"treeids": ""}
    tm = re.search(r"name=['\"]wbtreeids['\"][^>]*value=['\"]?([^'\">]*)",
                   m.group(1), re.I)
    return {"treeids": (tm.group(1).strip() if tm else "")}


class _ContentExtractor(HTMLParser):
    """提取 #vsb_content / .v_news_content 容器的正文。

    - `text()`               → 纯文本（与历史行为完全一致，图片不产生任何字符）
    - `body_and_images()`    → (纯文本, [{pos, url}])，pos 是图片在文本中的字符偏移，
                               用于按原页面顺序把图文重新拼成一条消息
    """

    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
    BREAK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "table", "section", "article", "blockquote"}
    BREAK_MARK = "\x00"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capturing = False
        self._depth = 0
        self._skip = 0
        self._parts: list[str] = []
        # 表格：正文里的表格会被拍平成文本（丢行列关系），所以额外记录原始 HTML，
        # 由渲染层转成图片，位置用与图片相同的 `pos`（正文字符偏移）机制。
        self._tables: list[str] = []
        self._table_buf: list[str] = []
        self._table_depth = 0

    def _mark_image(self, attrs) -> None:
        src = _pick_img_src({k.lower(): (v or "") for k, v in attrs})
        if src:
            self._parts.append(f"{_IMG_MARK}{src}{_IMG_END}")

    def handle_starttag(self, tag, attrs):
        if not self._capturing:
            d = {k.lower(): (v or "") for k, v in attrs}
            if d.get("id") == "vsb_content" or "v_news_content" in d.get("class", ""):
                self._capturing = True
                self._depth = 1
            return
        if tag == "img":
            if self._table_depth:
                self._append_table_tag("img", attrs, self_closing=True)
            else:
                self._mark_image(attrs)
            return
        if tag in self.VOID:
            if self._table_depth:
                self._append_table_tag(tag, attrs, self_closing=True)
            if tag == "br" and not self._table_depth:
                self._parts.append(self.BREAK_MARK)
            return
        if tag == "table" and not self._table_depth:
            # 进入表格：开始收集原始 HTML，并在正文里留一个表格占位标记
            self._table_depth = 1
            self._table_buf = [self._tag_html(tag, attrs)]
            self._depth += 1
            return
        if self._table_depth:
            self._table_depth += 1
            self._append_table_tag(tag, attrs)
            self._depth += 1
            return
        if tag in ("script", "style"):
            self._skip += 1
        self._depth += 1

    def handle_startendtag(self, tag, attrs):
        if not self._capturing:
            return
        if tag == "img":
            self._mark_image(attrs)
        elif tag == "br":
            self._parts.append(self.BREAK_MARK)

    def handle_endtag(self, tag):
        if not self._capturing or tag in self.VOID:
            return
        if self._table_depth:
            self._table_depth -= 1
            self._table_buf.append(f"</{tag}>")
            self._depth -= 1
            if self._table_depth == 0:
                # 表格结束：登记原始 HTML，正文里放标记（后续替换成图片）
                html = "".join(self._table_buf).strip()
                self._table_buf = []
                if html:
                    idx = len(self._tables)
                    self._tables.append(html)
                    self._parts.append(f"{_TBL_MARK}{idx}{_TBL_END}")
            if self._depth <= 0:
                self._capturing = False
            return
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        self._depth -= 1
        if tag in self.BREAK:
            self._parts.append(self.BREAK_MARK)
        if self._depth <= 0:
            self._capturing = False

    def handle_data(self, data):
        if not self._capturing or self._skip:
            return
        if self._table_depth:
            self._table_buf.append(data)
            return
        self._parts.append(data)

    @staticmethod
    def _tag_html(tag, attrs, self_closing: bool = False) -> str:
        parts = [tag]
        for k, v in attrs:
            v = "" if v is None else v
            parts.append(f'{k}="{v}"')
        return "<" + " ".join(parts) + ("/>" if self_closing else ">")

    def _append_table_tag(self, tag, attrs, self_closing: bool = False) -> None:
        self._table_buf.append(self._tag_html(tag, attrs, self_closing))

    @property
    def tables(self) -> list[str]:
        return list(self._tables)

    def marked_text(self) -> str:
        """规范化文本（仍带图片 sentinel）。"""
        raw = "".join(self._parts)
        raw = re.sub(r"\s+", " ", raw)          # 折叠源码里的缩进/换行
        raw = raw.replace(self.BREAK_MARK, "\n")
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw

    def text(self) -> str:
        t = _TABLE_SENTINEL_RE.sub("", self.marked_text())
        return _IMG_SENTINEL_RE.sub("", t).strip()

    def body_and_images(self) -> tuple[str, list[dict]]:
        """返回 (正文, [{"pos", "url"}])；pos 为图片前已产出的正文字符数。

        表格也会以 `{"pos", "table": <index>}` 混在同一个列表里返回（见
        `body_and_media()`），保证图文表三者的顺序与原文一致。
        """
        body, media = self.body_and_media()
        return body, [it for it in media if it.get("url")]

    def body_and_media(self) -> tuple[str, list[dict]]:
        """返回 (正文, [{pos, url} | {pos, table}])：图片与表格按原顺序混排。

        `pos` 是"该素材之前已产出的正文字符数"，与图片共用一套偏移语义，
        于是渲染层可以对图片和表格一视同仁。
        """
        marked = self.marked_text()
        if _IMG_MARK not in marked and _TBL_MARK not in marked:
            return marked.strip(), []
        media: list[dict] = []
        parts: list[str] = []
        pos = 0
        cursor = 0
        pattern = re.compile(
            rf"{re.escape(_IMG_MARK)}(.*?){re.escape(_IMG_END)}"
            rf"|{re.escape(_TBL_MARK)}(\d+){re.escape(_TBL_END)}")
        for m in pattern.finditer(marked):
            chunk = _tidy_chunk(marked[cursor:m.start()])
            parts.append(chunk)
            pos += len(chunk)
            if m.group(1) is not None:
                media.append({"pos": pos, "url": m.group(1)})
            else:
                media.append({"pos": pos, "table": int(m.group(2))})
            cursor = m.end()
        parts.append(_tidy_chunk(marked[cursor:]))
        return "".join(parts), media


def extract_body_text(html: str) -> str:
    p = _ContentExtractor()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        return ""
    return p.text()


def extract_body_with_tables(html: str, base_url: str = "") -> tuple[str, list[dict], list[str]]:
    """返回 (正文, [{pos,url}|{pos,table}], 表格 HTML 列表)。

    图片与表格**混在同一个列表**里按原文顺序返回，`pos` 语义一致
    （该素材之前已产出的正文字符数）。表格用 `table` 字段指向 `tables` 列表的下标——
    表本身是 HTML，不适合塞进正文，交给渲染层转图片。
    """
    p = _ContentExtractor()
    try:
        p.feed(html or "")
        p.close()
        raw_body, raw_media = p.body_and_media()
        tables = p.tables
    except Exception:
        return "", [], []
    lead = len(raw_body) - len(raw_body.lstrip())
    body = raw_body.strip()
    base = base_url or (PORTAL + "/")
    media: list[dict] = []
    for it in raw_media:
        pos = max(0, int(it.get("pos", 0)) - lead)
        if it.get("url") is not None:
            url = urljoin(base, (it.get("url") or "").strip())
            if url.startswith(("http://", "https://")):
                media.append({"pos": pos, "url": url})
            continue
        idx = it.get("table")
        if isinstance(idx, int) and 0 <= idx < len(tables):
            media.append({"pos": pos, "table": idx})
    return body, media, tables


def extract_body_with_images(html: str, base_url: str = "") -> tuple[str, list[dict]]:
    """返回 (正文, [{"pos", "url"}])：只有图片（表格被丢掉，兼容旧调用）。

    注意：这里返回的 `body` 与 `extract_body_text()` 一样做了首尾 strip，
    所以图片偏移要减去被剥掉的前导空白，保证 `body[pos:]` 仍是图片之后的正文。
    """
    body, media, _tables = extract_body_with_tables(html, base_url)
    return body, [it for it in media if it.get("url")]


def parse_detail(html: str, base_url: str = "") -> dict:
    """解析通知正文页，返回 {title, dept, date, body, images, tables}。

    `tables` 是正文里出现的表格原始 HTML 列表；`images` 里除了图片，还会有
    `{"pos", "table": <下标>}` 形式的表格引用（按原文顺序混排），
    渲染层据此把表格转成图片发送。
    """
    tm = _H1_RE.search(html or "")
    title = _text(tm.group(1)) if tm else ""
    if not title:
        m = _TITLE_TAG_RE.search(html or "")
        if m:
            title = re.sub(r"[-—|]\s*欢迎访问.*$", "", _text(m.group(1))).strip()

    plain = _text(html)
    dm = _DEPT_RE.search(plain)
    dt = _DATE_RE.search(plain)
    body, media, tables = extract_body_with_tables(html, base_url)
    return {
        "title": title,
        "dept": dm.group(1) if dm else "",
        "date": dt.group(1) if dt else "",
        "body": body,
        "images": media,
        "tables": tables,
    }


# ------------------------------------------------------------------ 客户端


class PortalClient:
    """带 CAS 自动重登的门户客户端。"""

    def __init__(
        self,
        username: str = "",
        password: str = "",
        logger=None,
        cookie_file: Optional[str] = None,
        timeout: int = 25,
    ) -> None:
        self.username = username or ""
        self.password = password or ""
        self.logger = logger
        self.cookie_file = cookie_file
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._authed = False

    # ---- 日志 ----

    def _log(self, level: str, msg: str, *args) -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)("[bupt_news] " + msg, *args)

    # ---- 会话 ----

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=10),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            if self.cookie_file and os.path.exists(self.cookie_file):
                self._load_cookies()
        return self._session

    def _load_cookies(self) -> None:
        from http.cookies import SimpleCookie

        try:
            with open(self.cookie_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        sc = SimpleCookie()
        for c in data.get("cookies", []):
            try:
                name = c.get("name")
                if not name:
                    continue
                sc[name] = c.get("value", "")
                if c.get("domain"):
                    sc[name]["domain"] = c["domain"]
                if c.get("path"):
                    sc[name]["path"] = c["path"]
            except Exception:
                continue
        if sc and self._session is not None:
            try:
                self._session.cookie_jar.update_cookies(sc)
            except Exception:
                pass

    def _save_cookies(self) -> None:
        if not self.cookie_file or self._session is None:
            return
        cookies = []
        try:
            for c in self._session.cookie_jar:
                cookies.append(
                    {
                        "name": c.key,
                        "value": c.value,
                        "domain": c.get("domain") or "",
                        "path": c.get("path") or "/",
                    }
                )
            d = os.path.dirname(self.cookie_file)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.cookie_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"savedAt": time.time(), "cookies": cookies}, f, ensure_ascii=False)
            os.replace(tmp, self.cookie_file)
            try:
                os.chmod(self.cookie_file, 0o600)
            except OSError:
                pass
        except Exception:
            pass

    # ---- 登录 ----

    async def login(self, force: bool = False) -> bool:
        """确保已登录。复用有效 Cookie，否则走 CAS 表单登录。

        校园网 DNS/连接偶发抖动，所以对网络类错误重试 3 次；
        认证类错误（账号密码不对）立即抛出，不做无意义重试。
        """
        async with self._lock:
            if self._authed and not force:
                return True
            if not self.username or not self.password:
                raise PortalAuthError(
                    "未配置门户账号/密码（plugins.bupt_news.username / password，"
                    "或环境变量 BUPT_PORTAL_USERNAME / BUPT_PORTAL_PASSWORD）"
                )
            last: Optional[Exception] = None
            for attempt in (1, 2, 3):
                try:
                    return await self._login_once()
                except PortalAuthError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last = e
                    self._log("warning", "登录请求失败（第 %d/3 次）：%s", attempt, e)
                    if attempt < 3:
                        await asyncio.sleep(2.0 * attempt)
            raise PortalError(f"门户登录失败（网络不可达）：{last}")

    async def _login_once(self) -> bool:
        session = await self._ensure_session()

        # 门户根 -> (302) clogin.jsp -> (302) CAS 登录页
        async with session.get(PORTAL + "/") as resp:
            html = await resp.text(errors="replace")
            final = str(resp.url)

        if CAS_MARK not in final:
            self._authed = True              # 持久化 Cookie 仍然有效
            self._save_cookies()
            return True

        execution = _input_value(html, "execution")
        if not execution:
            raise PortalAuthError("CAS 登录页未找到 execution token（页面结构可能已变更）")

        form = {
            "username": self.username,
            "password": self.password,
            "type": "username_password",
            "execution": execution,
            "_eventId": "submit",
            "submit": "LOGIN",
        }
        origin = "{0.scheme}://{0.netloc}".format(urlparse(final))
        async with session.post(
            final,
            data=form,
            headers={"Referer": final, "Origin": origin},
            allow_redirects=True,
        ) as resp:
            text = await resp.text(errors="replace")
            end = str(resp.url)

        self._authed = False
        if CAS_MARK in end or "loginForm" in text:
            raise PortalAuthError(
                "CAS 登录被拒绝：账号或密码错误，或触发了验证码/二次验证"
            )
        self._authed = True
        self._save_cookies()
        self._log("info", "门户登录成功 (%s)", self.username)
        return True

    # ---- 取页 ----

    async def _get(self, url: str, params: Optional[dict] = None) -> tuple[str, str]:
        """GET 一个页面，遇到 CAS 重定向时自动重登一次。"""
        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            session = await self._ensure_session()
            try:
                async with session.get(url, params=params) as resp:
                    text = await resp.text(errors="replace")
                    final = str(resp.url)
                    status = resp.status
            except aiohttp.ClientError as e:
                last_err = e
                if attempt == 2:
                    raise PortalError(f"请求失败: {url} ({e})") from e
                await asyncio.sleep(1.0)
                continue

            if status >= 500 and attempt == 1:
                await asyncio.sleep(1.0)
                continue
            if CAS_MARK in final:
                self._authed = False
                if attempt == 2:
                    raise PortalAuthError("门户会话失效，重新登录后仍被重定向到 CAS")
                await self.login(force=True)
                continue
            return text, final
        raise PortalError(f"请求失败: {url} ({last_err})")

    # ---- 业务 ----

    async def fetch_list(self, column, pages: int = 1) -> list[Notice]:
        """抓取栏目第 1 页（最新 20 条），可选多页。

        注意：list.jsp 的 totalpage 是"渲染分页控件的输入"，首页必须省略它，
        否则服务端只会渲染单页且分页链接全部消失。
        """
        base_params = {"urltype": "tree.TreeTempUrl", "wbtreeid": str(column)}
        html, _ = await self._get(PORTAL + "/list.jsp", params=base_params)
        notices = parse_list(html, column)
        total = parse_total_pages(html)

        want = max(1, min(int(pages), total))
        for p in range(2, want + 1):
            params = dict(base_params, totalpage=str(total), PAGENUM=str(p))
            html, _ = await self._get(PORTAL + "/list.jsp", params=params)
            known = {n.key for n in notices}
            for n in parse_list(html, column):
                if n.key not in known:
                    notices.append(n)
                    known.add(n.key)
            await asyncio.sleep(0.3)
        return notices

    async def search(self, keyword: str, *, page: int = 1, page_size: int = 10,
                     content: bool = True, column: str = "1154") -> dict:
        """在信息门户里检索通知，返回 `{total, page, pages, size, items}`。

        门户检索（`fz_ssjg.jsp`）的协议要点（都是逆向页面得来的，改动前先核对）：

        * 提交走 **POST**：`INTEXT2` 是关键词的 **base64**，`Find=find`、
          `entrymode=2`、`wbnewsfield` 选标题(`wbtitle`)/全文(`wbcontent`)；
        * `wbtreeids` 是栏目树 id 列表，**必须**先从列表页的表单里抄过来
          （写死会漏栏目，门户调整栏目时也会静默失效）；
        * 翻页不能用 POST（服务端只认页面自报的私有参数），必须走 GET：
          `?wbtreeid=..&wbnewsfield=..&Find=find&entrymode=2&a218493i=<kw>`
          `&a218493c=<每页>&a218493p=<页码>&a218493t=<总页数>`；
        * 每页条数由 `a218493c` 决定，但**服务端最多给 10 条**。

        ⚠️ 实测无验证码（`NewsSearchDWR.isSearch` 那条链路是给浏览器用的），
        空 `news_search_code` 也能拿到结果 —— 但门户随时可能加闸，失败要能降级。
        """
        kw = (keyword or "").strip()
        if not kw:
            return {"total": 0, "page": 1, "pages": 0, "size": int(page_size), "items": []}
        page = max(1, int(page or 1))
        size = max(1, min(int(page_size or 10), 10))     # 服务端上限 10
        field = "wbcontent" if content else "wbtitle"

        if page == 1:
            # 首页：POST（顺带拿到总条数）
            html, _ = await self._get(PORTAL + "/list.jsp",
                                      params={"urltype": "tree.TreeTempUrl",
                                              "wbtreeid": str(column)})
            form = _search_form(html, column)
            b64 = base64.b64encode(kw.encode("utf-8")).decode("ascii")
            payload = {"wbnewsfield": field, "Find": "find", "entrymode": "2",
                       "INTEXT2": b64, "news_search_code": "",
                       "wbtreeids": form["treeids"]}
            session = await self._ensure_session()
            async with session.post(
                    f"{PORTAL}/fz_ssjg.jsp?wbtreeid={int(column)}",
                    data=payload) as resp:
                body = await resp.text()
        else:
            # 翻页：GET（参数见上面说明）。
            # `a218493t` 只是页面用来算"后面还有几页"的总页数；给一个足够大的值
            # 不会导致越界（服务端按页码取数据），所以不必为了它再发一次首页请求。
            params = {"wbtreeid": str(column), "wbnewsfield": field,
                      "Find": "find", "entrymode": "2",
                      "a218493i": kw, _SEARCH_PAGE_SIZE_PARAM: str(size),
                      _SEARCH_PAGE_PARAM: str(page),
                      _SEARCH_TOTAL_PARAM: "999"}
            body, _ = await self._get(PORTAL + "/fz_ssjg.jsp", params=params)

        items = parse_search_results(body)
        total = parse_search_total(body)
        if page == 1 and not total:
            total = len(items)
        pages = max(1, -(-total // size)) if total else (1 if items else 0)
        return {"total": total, "page": page, "pages": pages, "size": size,
                "items": items}

    async def fetch_detail(self, url: str) -> dict:
        html, _ = await self._get(url)
        return parse_detail(html, url)

    async def fetch_bytes(self, url: str) -> tuple[bytes, str]:
        """下载二进制内容（正文图片）。复用同一个已登录会话与 CAS 重登逻辑。"""
        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            session = await self._ensure_session()
            try:
                async with session.get(url) as resp:
                    data = await resp.read()
                    status = resp.status
                    final = str(resp.url)
                    ctype = resp.headers.get("Content-Type", "")
            except aiohttp.ClientError as e:
                last_err = e
                if attempt == 2:
                    raise PortalError(f"下载失败: {url} ({e})") from e
                await asyncio.sleep(1.0)
                continue

            if status >= 500 and attempt == 1:
                await asyncio.sleep(1.0)
                continue
            if status >= 400:
                raise PortalError(f"下载失败: HTTP {status} {url}")
            if CAS_MARK in final:
                # 图片地址被重定向回登录页：会话失效，重登一次再试
                self._authed = False
                if attempt == 2:
                    raise PortalAuthError("门户会话失效（下载图片时）")
                await self.login(force=True)
                continue
            return data, ctype
        raise PortalError(f"下载失败: {url} ({last_err})")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._authed = False
