"""正文表格 → 图片。

为什么需要它：门户正文里的表格被正文提取器**拍平成文本**（每个单元格一行），
行列对应关系会彻底丢失。例如 2026 级新生疫苗接种通知里的排期表会变成

    时间 / 10月10日 / 10月18日 / 9:00-9:30 / 经济管理学院 / 物理科学与技术学院…

根本看不出哪个学院属于哪个时段。所以表格必须**按表格渲染成图片**再发出去，
而不是靠文本拼。

渲染用 chromium headless（与原文一致的排版：border-collapse、rowspan、
中文字体都在浏览器里天然正确），再用 PIL 裁掉四周空白：

    chromium --dump-dom   → 量出表格实际尺寸（注入脚本写进 <title>）
    chromium --screenshot → 截图，然后按非白像素 bbox 裁剪

只保留表格本身，不带正文其他内容；样式沿用原文（内联 style/class 保留）。
"""
from __future__ import annotations

import asyncio
import hashlib
import html as _html
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable, Optional

# 表格里的 `table://<id>` 引用（与图片的 http(s) URL 区分开）
TABLE_SCHEME = "table://"

# 渲染尺寸上限：避免超大表格把消息撑爆或截图超时
MAX_WIDTH = 1400
MAX_HEIGHT = 4000
FONT_SIZE = 17
CELL_PADDING = 6
BROWSER_TIMEOUT = 25.0

_CHROMIUM_CANDIDATES = ("chromium", "chromium-browser", "google-chrome",
                        "google-chrome-stable", "chrome")

# 主文档 CSS：只负责隔离与缩放到可读宽度，表格自身样式（边框/对齐）保持原样
_PAGE_CSS = """
html, body {{ margin:0; padding:0; background:#fff; }}
#__wrap {{ display:inline-block; padding:{pad}px; background:#fff; }}
#__tbl table {{ border-collapse:collapse; }}
#__tbl td, #__tbl th {{ padding:{cellpad}px {padx}px; }}
#__tbl img {{ max-width:{maxw}px; height:auto; }}
"""


# ------------------------------------------------------------------ 解析


@dataclass
class TableCell:
    """一个单元格在网格中的位置。"""

    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    html: str = ""          # 单元格内部 HTML（保留内联样式/链接文字）
    is_header: bool = False
    is_placeholder: bool = False    # 被 rowspan/colspan 覆盖出的占位格

    @property
    def text(self) -> str:
        return _plain(self.html)


@dataclass
class Table:
    """一张表格：单元格网格 + 原始 HTML。"""

    rows: int
    cols: int
    cells: list[TableCell] = field(default_factory=list)
    html: str = ""

    @property
    def empty(self) -> bool:
        return self.rows <= 0 or self.cols <= 0

    @property
    def text_len(self) -> int:
        """去掉占位格后的可见文字长度（用于判断"空表"）。"""
        return sum(len(c.text) for c in self.cells if not c.is_placeholder)

    def signature(self) -> str:
        """内容指纹：用于稳定命名与去重。"""
        raw = self.html or self.text_content()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def text_content(self) -> str:
        return "\n".join(
            " | ".join(c.text for c in self.cells
                       if c.row == r and not c.is_placeholder)
            for r in range(self.rows))


class _TableParser(HTMLParser):
    """把一段（已由浏览器规范化的）表格 HTML 解析成网格。

    必须能处理 rowspan/colspan —— 接种排期表就用了 `rowspan=3`
    （"计算机学院"跨三行）。占位格也要显式建出来，否则后续列会错位。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._cells: list[TableCell] = []
        self._row = -1
        self._col = 0
        self._span = 1                # 当前单元格的 colspan
        self._span_row = 1
        self._is_header = False
        self._buf: list[str] = []
        self._in_cell = False
        self._skip = 0
        self._occupied: dict[tuple[int, int], bool] = {}

    # -- 网格占位 --
    def _next_free_col(self) -> int:
        col = self._col
        while self._occupied.get((self._row, col)):
            col += 1
        return col

    def _occupy(self, row: int, col: int, rowspan: int, colspan: int) -> None:
        for r in range(row, row + rowspan):
            for c in range(col, col + colspan):
                self._occupied[(r, c)] = True

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "tr":
            self._row += 1
            self._col = 0
            return
        if tag in ("td", "th"):
            self._col = self._next_free_col()
            self._span = _to_int(d.get("colspan"), 1)
            self._span_row = _to_int(d.get("rowspan"), 1)
            self._is_header = (tag == "th")
            self._buf = []
            self._in_cell = True
            self._occupy(self._row, self._col, self._span_row, self._span)
            # 记录单元格起点：先把占位格补全（被 rowspan 覆盖的后续行）
            return
        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._in_cell and tag == "br":
            self._buf.append("<br>")
            return
        if self._in_cell and tag == "img":
            # 表格里的图片：保留 src，由浏览器的 max-width 约束缩放
            src = d.get("src") or d.get("data-src") or ""
            if src:
                self._buf.append(f'<img src="{_html.escape(src, quote=True)}">')
            return
        if self._in_cell and tag in ("a", "span", "b", "strong", "em", "i",
                                     "u", "font", "sub", "sup", "small"):
            # 保留少量内联标签（链接/加粗等）
            keep = " ".join(f'{k}="{_html.escape(v, quote=True)}"'
                            for k, v in d.items() if k in ("style", "href", "color"))
            self._buf.append(f"<{tag}{(' ' + keep) if keep else ''}>")
            return

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
            return
        if tag in ("td", "th") and self._in_cell:
            self._cells.append(TableCell(
                row=self._row, col=self._col,
                rowspan=self._span_row, colspan=self._span,
                html="".join(self._buf).strip(),
                is_header=self._is_header))
            self._in_cell = False
            self._buf = []
            return
        if self._in_cell and tag in ("a", "span", "b", "strong", "em", "i",
                                     "u", "font", "sub", "sup", "small"):
            self._buf.append(f"</{tag}>")

    def handle_data(self, data):
        if self._in_cell and not self._skip:
            self._buf.append(_html.escape(data, quote=False))


def _to_int(v, default: int) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _plain(s: str) -> str:
    """把单元格 HTML 压成纯文本（判断空表/日志用）。"""
    t = re.sub(r"<br\s*/?>", " ", s or "", flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()


def parse_table(table_html: str) -> Optional[Table]:
    """解析表格 HTML → Table；解析不出内容返回 None。"""
    if not table_html:
        return None
    p = _TableParser()
    try:
        p.feed(table_html)
        p.close()
    except Exception:  # noqa: BLE001 - 脏 HTML 不该让整条通知失败
        return None
    cells = p._cells
    if not cells:
        return None
    rows = max(c.row for c in cells) + 1
    cols = max((c.col + c.colspan) for c in cells)
    if rows <= 0 or cols <= 0:
        return None

    # 铺满 rows×cols 网格：起点格放真实单元格，被 span 覆盖的位置放占位格。
    # 占位格不是"空格子"，而是"这一格归上面的 rowspan 单元格所有"，
    # 渲染时跳过；缺了它后续列会整体错位。
    origin = {(c.row, c.col): c for c in cells}
    grid: dict[tuple[int, int], TableCell] = {}
    for r in range(rows):
        for c in range(cols):
            cell = origin.get((r, c))
            if cell is not None:
                grid[(r, c)] = cell
                continue
            grid[(r, c)] = TableCell(row=r, col=c, is_placeholder=True)
    t = Table(rows=rows, cols=cols,
              cells=[grid[(r, c)] for r in range(rows) for c in range(cols)],
              html=table_html)
    # 空表 / 纯布局表：一个字都没有就别渲染成图片
    if t.text_len == 0 and "<img" not in (table_html or "").lower():
        return None
    return t


def is_renderable(table: Table) -> bool:
    """是否值得渲染成图片。

    过滤掉两类噪声（门户页面里真实存在）：
      * 工具栏表（"打印" 之类，只有 1 行且字数很少）
      * 纯布局用的空表（没有任何文字与图片）
    """
    if table.empty:
        return False
    if table.text_len == 0 and "<img" not in (table.html or "").lower():
        return False
    if table.rows == 1 and table.cols <= 2 and table.text_len <= 8:
        return False
    return True


# ------------------------------------------------------------------ 渲染


def find_browser() -> Optional[str]:
    """找一个可用的 chromium。"""
    for name in _CHROMIUM_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _page_html(table_html: str) -> str:
    """把表格包成一个独立页面：去掉外部资源，注入量尺寸的脚本。

    * 去掉 `<script>`：不让原文脚本在本地页里执行；
    * 去掉 `@import` / 外链 `<link>`：避免为了样式去联网（离线环境会挂住）；
    * 量出 `#__wrap` 的实际尺寸写进 `<title>`，截图后据此裁剪。
    """
    body = _sanitize(table_html)
    script = (
        "window.addEventListener('load', function(){"
        " var w=document.getElementById('__wrap');"
        " var r=w.getBoundingClientRect();"
        " document.title=Math.ceil(r.width)+'x'+Math.ceil(r.height);"
        "});"
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_PAGE_CSS.format(pad=8, cellpad=CELL_PADDING, padx=10, maxw=MAX_WIDTH)}</style>"
        f"</head><body><div id='__wrap'><div id='__tbl'>{body}</div></div>"
        f"<script>{script}</script></body></html>"
    )


def _sanitize(table_html: str) -> str:
    """去掉会触发网络/脚本的部分，其余（内联样式、class、结构）保持原样。"""
    s = table_html or ""
    s = re.sub(r"<script\b.*?</script>", "", s, flags=re.S | re.I)
    s = re.sub(r"<link\b[^>]*>", "", s, flags=re.I)
    s = re.sub(r"@import[^;]+;", "", s, flags=re.I)
    # 外部图片会拖慢/挂住渲染：留成 alt 文本占位
    s = re.sub(r"<img\b[^>]*?(?:src|data-src)=[\"']([^\"']+)[\"'][^>]*>",
               lambda m: (m.group(0) if not m.group(1).lower().startswith(
                   ("http://", "https://", "//")) else "<span>[图片]</span>"),
               s, flags=re.I)
    return s


async def _run_browser(browser: str, page: str, *extra: str,
                       profile_dir: str) -> tuple[int, str]:
    """跑一次 chromium，返回 (returncode, stdout)。"""
    with tempfile.TemporaryDirectory(prefix="tblpage-", dir=profile_dir) as td:
        page_path = os.path.join(td, "page.html")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(page)
        cmd = [browser, "--headless", "--no-sandbox", "--disable-gpu",
               "--disable-dev-shm-usage", "--hide-scrollbars",
               "--force-device-scale-factor=1",
               f"--user-data-dir={os.path.join(td, 'profile')}",
               *extra, f"file://{page_path}"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), BROWSER_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, ""
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


async def render_table_png(table_html: str, out_path: str, *,
                           browser: Optional[str] = None,
                           work_dir: str) -> Optional[str]:
    """把表格渲染成 PNG，写入 `out_path`；成功返回路径。

    两步：`--dump-dom` 量尺寸（把尺寸写进 <title>），再按该尺寸 `--screenshot`。
    截图后仍按非白像素裁剪一次，去掉浏览器的默认外边距。
    """
    browser = browser or find_browser()
    if not browser:
        return None
    page = _page_html(table_html)
    os.makedirs(work_dir, exist_ok=True)

    # 1) 量尺寸
    rc, dom = await _run_browser(browser, page, "--dump-dom", profile_dir=work_dir)
    if rc != 0 or not dom:
        return None
    m = re.search(r"<title>(\d+)x(\d+)</title>", dom)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
    else:
        width, height = 0, 0            # 脚本没跑（老浏览器）→ 用兜底尺寸再裁
    width = min(max(width or 900, 120), MAX_WIDTH + 40)
    height = min(max(height or 400, 80), MAX_HEIGHT)

    # 2) 截图
    shot = os.path.join(work_dir, f"shot-{os.getpid()}-{abs(hash(page)) % 10**8}.png")
    rc, _ = await _run_browser(
        browser, page, f"--screenshot={shot}",
        f"--window-size={width},{height}", profile_dir=work_dir)
    if rc != 0 or not os.path.exists(shot) or os.path.getsize(shot) == 0:
        return None

    # 3) 裁掉四周空白 + 存为目标文件
    if not _crop_to_content(shot, out_path):
        os.replace(shot, out_path)      # 裁不了就直接用原图
    else:
        try:
            os.unlink(shot)
        except OSError:
            pass
    return out_path if os.path.getsize(out_path) > 0 else None


def _crop_to_content(src: str, dst: str) -> bool:
    """按非白像素 bbox 裁剪（顺带留 6px 白边）。失败返回 False。"""
    try:
        from PIL import Image, ImageChops
    except Exception:  # noqa: BLE001 - 没有 PIL 就退回不裁
        return False
    try:
        im = Image.open(src).convert("RGB")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bbox = ImageChops.difference(im, bg).getbbox()
        if not bbox:
            return False
        pad = 6
        left = max(0, bbox[0] - pad)
        top = max(0, bbox[1] - pad)
        right = min(im.width, bbox[2] + pad)
        bottom = min(im.height, bbox[3] + pad)
        im.crop((left, top, right, bottom)).save(dst, "PNG", optimize=True)
        return True
    except Exception:  # noqa: BLE001
        return False


class TableImageStore:
    """表格 → 图片的缓存目录：`<data_dir>/tables/<内容哈希>.png`。

    按**内容哈希**命名：同一个表格（比如被多轮重抓的同一条通知）只渲染一次；
    内容变了哈希就变，天然不会拿到旧图 —— 不需要额外的索引文件。
    """

    def __init__(self, directory: str, *, enabled: bool = True, logger=None,
                 browser: Optional[str] = None) -> None:
        self.directory = directory
        self.enabled = bool(enabled)
        self.logger = logger
        self.browser = browser or find_browser()

    @property
    def available(self) -> bool:
        """是否具备渲染能力（开关开着 + 找得到 chromium）。"""
        return self.enabled and bool(self.browser)

    def _log(self, level: str, msg: str, *args) -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)("[bupt_news] " + msg, *args)

    def path_for(self, table_html: str) -> str:
        digest = hashlib.sha256((table_html or "").encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.directory, f"{digest}.png")

    def find(self, table_html: str) -> Optional[str]:
        if not table_html:
            return None
        path = self.path_for(table_html)
        return path if os.path.isfile(path) and os.path.getsize(path) > 0 else None

    async def ensure(self, table_html: str) -> Optional[str]:
        """确保该表格已渲染成 PNG，返回本地路径；不可用/失败返回 None。"""
        if not self.available or not table_html:
            return None
        cached = self.find(table_html)
        if cached:
            return cached
        out = self.path_for(table_html)
        os.makedirs(self.directory, exist_ok=True)
        path = await render_table_png(table_html, out, browser=self.browser,
                                      work_dir=self.directory)
        if path:
            self._log("info", "表格已渲染为图片：%s", os.path.basename(path))
        else:
            self._log("warning", "表格渲染失败（%d 字节 HTML），改用文本形式",
                      len(table_html))
        return path

    def prune(self, keep: Iterable[str]) -> int:
        """删除不再被引用的表格图片（`keep` 是仍在用的路径集合）。"""
        keep_set = {os.path.basename(p) for p in keep if p}
        removed = 0
        try:
            names = os.listdir(self.directory)
        except OSError:
            return 0
        for name in names:
            if not name.endswith(".png") or name in keep_set:
                continue
            try:
                os.unlink(os.path.join(self.directory, name))
                removed += 1
            except OSError:
                pass
        return removed

    def stats(self) -> tuple[int, int]:
        """(文件数, 总字节数)。"""
        files = size = 0
        try:
            for name in os.listdir(self.directory):
                if not name.endswith(".png"):
                    continue
                p = os.path.join(self.directory, name)
                if os.path.isfile(p):
                    files += 1
                    size += os.path.getsize(p)
        except OSError:
            pass
        return files, size
