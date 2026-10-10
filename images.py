"""通知正文图片的按需下载 + 本地缓存。

为什么要落盘而不是把 URL 丢给 NapCat：

* 门户图片需要校园网 + 登录 Cookie（`my.bupt.edu.cn`），NapCat 那边不一定具备；
* 同一条通知可能发往多个群/人，落盘后只下载一次；
* 本地文件路径是最稳的 OneBot `image` 段写法（`file` 指向磁盘文件）。

缓存文件名 = `sha1(url)[:16] + 扩展名`，扩展名优先按文件头魔数判定（门户经常返回
`application/octet-stream`），其次看 Content-Type，最后看 URL 后缀。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Iterable, Optional

from .portal import PortalAuthError

DEFAULT_MAX_BYTES = 10 * 1024 * 1024        # 单张图上限 10MB

_MAGIC_EXT = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),                     # 需再看第 8-12 字节，见 _sniff_ext
    (b"BM", ".bmp"),
)
_CTYPE_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
    "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/bmp": ".bmp", "image/x-ms-bmp": ".bmp",
}
_URL_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|bmp)(?:$|[?#])", re.I)


def image_key(url: str) -> str:
    """URL → 缓存文件名前缀（稳定、无路径穿越风险）。"""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def _url_ext(url: str) -> str:
    m = _URL_EXT_RE.search(url or "")
    if not m:
        return ""
    ext = m.group(1).lower()
    return ".jpg" if ext in ("jpeg", "jpg") else f".{ext}"


def _sniff_ext(data: bytes, ctype: str = "") -> str:
    """按内容判定扩展名；判不出来返回空串（调用方据此丢弃非图片响应）。"""
    for magic, ext in _MAGIC_EXT:
        if data.startswith(magic):
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            return ext
    ct = (ctype or "").split(";")[0].strip().lower()
    return _CTYPE_EXT.get(ct, "")


class ImageCache:
    """把 `<data_dir>/images/` 当作图片缓存目录，按 URL 去重。

    `portal_getter` 每次取当前的门户客户端（插件停用/重载后会换实例）。
    """

    def __init__(self, directory: str, portal_getter=None, *, enabled: bool = True,
                 max_bytes: int = DEFAULT_MAX_BYTES, logger=None,
                 min_prune_age: float = 3600.0) -> None:
        self.directory = directory
        self.portal_getter = portal_getter or (lambda: None)
        self.enabled = bool(enabled)
        self.max_bytes = max(0, int(max_bytes or 0))
        self.logger = logger
        self.min_prune_age = max(0.0, float(min_prune_age))

    # ---- 日志 ----

    def _log(self, level: str, msg: str, *args) -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)("[bupt_news] " + msg, *args)

    # ---- 路径 ----

    def find(self, url: str) -> Optional[str]:
        """返回已缓存的本地路径（不存在返回 None）。扩展名可能来自魔数，故按前缀找。"""
        key = image_key(url)
        try:
            names = os.listdir(self.directory)
        except OSError:
            return None
        for name in names:
            if name.startswith(key) and not name.endswith(".tmp"):
                path = os.path.join(self.directory, name)
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    return path
        return None

    def cached_keys(self) -> set[str]:
        """已缓存的 URL key 集合（一次 listdir，批量判断缺失用）。"""
        keys: set[str] = set()
        try:
            names = os.listdir(self.directory)
        except OSError:
            return keys
        for name in names:
            if name.endswith(".tmp"):
                continue
            try:
                if os.path.getsize(os.path.join(self.directory, name)) > 0:
                    keys.add(name.split(".", 1)[0])
            except OSError:
                continue
        return keys

    # ---- 下载 ----

    async def ensure(self, url: str) -> Optional[str]:
        """确保 url 对应的图片已落盘，返回本地路径；不可用则返回 None。"""
        if not url:
            return None
        cached = self.find(url)
        if cached:
            return cached
        portal = self.portal_getter() if self.enabled else None
        if portal is None:
            return None
        try:
            data, ctype = await portal.fetch_bytes(url)
        except PortalAuthError:
            raise
        except Exception as e:
            self._log("warning", "图片下载失败（%s）：%s", url, e)
            return None

        if self.max_bytes and len(data) > self.max_bytes:
            self._log("warning", "图片超过 %d 字节，已跳过：%s", self.max_bytes, url)
            return None
        ext = _sniff_ext(data, ctype) or _url_ext(url)
        if not data:
            self._log("warning", "图片内容为空，已跳过：%s", url)
            return None
        if not ext:
            # 既不是已知图片魔数、也没有图片 Content-Type/后缀 → 多半是登录页 HTML
            self._log("warning", "响应不是图片（ctype=%s），已跳过：%s", ctype or "-", url)
            return None

        path = os.path.join(self.directory, image_key(url) + ext)
        try:
            os.makedirs(self.directory, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError as e:
            self._log("warning", "图片写入失败（%s）：%s", path, e)
            return None
        self._log("debug", "图片已缓存 %s (%d 字节)", path, len(data))
        return path

    # ---- 清理 ----

    def prune(self, keep_urls: Iterable[str]) -> int:
        """删除不再被任何通知引用的缓存文件，返回删除数量。

        刚下载的文件（`min_prune_age` 秒内）不动，避免与正在发送的消息抢文件。
        """
        keep = {image_key(u) for u in keep_urls if u}
        try:
            names = os.listdir(self.directory)
        except OSError:
            return 0
        removed = 0
        now = time.time()
        for name in names:
            path = os.path.join(self.directory, name)
            if name.endswith(".tmp"):
                pass
            elif name.split(".", 1)[0] in keep:
                continue
            try:
                if not os.path.isfile(path):
                    continue
                if now - os.path.getmtime(path) < self.min_prune_age:
                    continue
                os.remove(path)
                removed += 1
            except OSError:
                continue
        if removed:
            self._log("info", "图片缓存已清理 %d 个文件", removed)
        return removed

    def stats(self) -> tuple[int, int]:
        """返回 (文件数, 总字节数)。"""
        count = size = 0
        try:
            names = os.listdir(self.directory)
        except OSError:
            return 0, 0
        for name in names:
            try:
                p = os.path.join(self.directory, name)
                if os.path.isfile(p):
                    count += 1
                    size += os.path.getsize(p)
            except OSError:
                continue
        return count, size


class FailureLog:
    """下载失败账本（`<data_dir>/image_failures.json`），用于「失败后重试」。

    记录每个 URL 的累计失败次数、最后一次错误与时间：

    * 自动补齐：累计失败达到 `max_attempts` 后不再每轮空撞（可用命令强制重试）；
    * 手动重试：`/buptnews images retry` 忽略次数上限，直接再试一遍；
    * 下载成功即从账本里移除。
    """

    MAX_ENTRIES = 2000

    def __init__(self, path: str, logger=None) -> None:
        self.path = path
        self.logger = logger
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as e:
            if self.logger:
                self.logger.warning("[bupt_news] 失败账本写入失败（%s）：%s", self.path, e)

    # ---- 读写 ----

    def note_failure(self, url: str, error: str = "") -> int:
        """记一次失败，返回累计失败次数。"""
        rec = self._data.setdefault(url, {})
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["last"] = time.time()
        rec["error"] = str(error)[:200]
        self._dirty = True
        self._trim()
        return rec["attempts"]

    def note_success(self, url: str) -> None:
        if self._data.pop(url, None) is not None:
            self._dirty = True

    def attempts(self, url: str) -> int:
        return int((self._data.get(url) or {}).get("attempts", 0))

    def error_of(self, url: str) -> str:
        return str((self._data.get(url) or {}).get("error", ""))

    def urls(self) -> list[str]:
        return list(self._data)

    def count(self) -> int:
        return len(self._data)

    def last_error(self) -> str:
        if not self._data:
            return ""
        rec = max(self._data.values(), key=lambda r: float(r.get("last", 0)))
        return str(rec.get("error", ""))

    def clear(self) -> None:
        if self._data:
            self._data.clear()
            self._dirty = True

    def _trim(self) -> None:
        """账本按最近失败时间保留，避免无限增长。"""
        if len(self._data) <= self.MAX_ENTRIES:
            return
        keep = sorted(self._data.items(),
                      key=lambda kv: float(kv[1].get("last", 0)), reverse=True)
        self._data = dict(keep[:self.MAX_ENTRIES])
