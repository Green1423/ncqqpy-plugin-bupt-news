"""图片补齐：给「加图片功能之前入库」的历史消息补上正文图片。

两件事分开做，因为代价差很多：

* `recrawl_missing_images()` —— 对**记录里没有 `images` 字段**的历史消息重抓正文页，
  补出图片 URL 与它在正文中的位置（每条一次 HTTP 请求，慢，按条限流、分批落盘）；
* `download_missing_images()` —— 把「记录引用但本地缓存里没有」的图片下载下来
  （新记录优先，其余按记录 id 从新到旧，可限制单轮张数）。

同步流程与 `/buptnews images backfill` 命令、以及一次性脚本 `backfill_images.py`
共用这里的实现，避免三处各写一套。
"""
from __future__ import annotations

import asyncio
from typing import Iterable, Optional

from .images import FailureLog, ImageCache, image_key

LOGGER_PREFIX = "[bupt_news] "

EMPTY_DOWNLOAD_STATS = {"missing": 0, "tried": 0, "ok": 0, "failed": 0,
                        "remaining": 0, "skipped": 0}


def _log(logger, level: str, msg: str, *args) -> None:
    if logger:
        getattr(logger, level, logger.info)(LOGGER_PREFIX + msg, *args)


def records_missing_images(store) -> list[dict]:
    """正文素材抓取不完整、但有 url 的记录（旧 → 新）。

    两个补抓目标：
      * 没有 `images` 字段 —— 加图片功能之前入库；
      * 没有 `tables` 字段 —— 加表格转图片功能之前入库（**即使有 images**）。

    两者都靠重抓正文页补齐，所以合成同一批候选，避免同一页被抓两遍。
    """
    return [r for r in store.all_records()
            if ("images" not in r or "tables" not in r)
            and (r.get("url") or "").strip()]


def collect_referenced_urls(store, fresh_urls: Optional[Iterable[str]] = None) -> list[str]:
    """记录引用到的图片 URL：本轮新通知优先，其余按记录 id 从新到旧去重。"""
    urls: list[str] = []
    seen: set[str] = set()

    def add(url) -> None:
        u = str(url or "")
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    for u in fresh_urls or ():
        add(u)
    for rec in reversed(store.all_records()):        # 新 → 旧
        for it in (rec.get("images") or []):
            if isinstance(it, dict):
                add(it.get("url"))
    return urls


def missing_image_urls(store, images: ImageCache,
                       fresh_urls: Optional[Iterable[str]] = None) -> list[str]:
    """记录引用、但本地缓存里还没有的图片 URL。"""
    if images is None or not images.enabled:
        return []
    cached = images.cached_keys()
    return [u for u in collect_referenced_urls(store, fresh_urls)
            if image_key(u) not in cached]


async def download_missing_images(store, images: ImageCache, *,
                                  fresh_urls: Optional[Iterable[str]] = None,
                                  limit: int = 0, pause: float = 0.2,
                                  failures: Optional[FailureLog] = None,
                                  retry: int = 0, retry_delay: float = 2.0,
                                  max_attempts: int = 0, force: bool = False,
                                  only_failed: bool = False,
                                  logger=None) -> dict:
    """下载缺失的图片；`limit=0` 表示不限。返回统计字典。

    失败重试：
    * `retry`：每张图失败后再立刻重试几次，间隔 `retry_delay × 2^k`（秒，指数递增）；
    * `failures`：失败账本。成功的移除记录；失败的累计次数，
      达到 `max_attempts` 后自动补齐不再尝试（`force=True` 或命令重试可忽略该上限）；
    * `only_failed=True`：只重试账本里失败过的 URL（手动重试命令用）。
    """
    if images is None or not images.enabled:
        return dict(EMPTY_DOWNLOAD_STATS)

    candidates = missing_image_urls(store, images, fresh_urls)
    if only_failed and failures is not None:
        failed_set = set(failures.urls())
        candidates = [u for u in candidates if u in failed_set]
    if not candidates:
        return dict(EMPTY_DOWNLOAD_STATS)

    skipped = 0
    if failures is not None and max_attempts > 0 and not force:
        keep = []
        for url in candidates:
            if failures.attempts(url) >= max_attempts:
                skipped += 1
            else:
                keep.append(url)
        candidates = keep
    todo = candidates if limit <= 0 else candidates[:limit]

    ok = failed = 0
    for url in todo:
        path = None
        last_err = ""
        for attempt in range(1, max(1, retry + 1) + 1):
            try:
                path = await images.ensure(url)
            except Exception as e:  # noqa: BLE001 — 单张失败不影响其它
                last_err = f"{type(e).__name__}: {e}"
                path = None
            if path:
                break
            if failures is not None:
                failures.note_failure(url, last_err or "未取到图片内容")
            if attempt <= retry:
                delay = retry_delay * (2 ** (attempt - 1))
                _log(logger, "info", "图片下载失败，%.1fs 后重试（%d/%d）：%s",
                     delay, attempt, retry, url)
                if delay > 0:
                    await asyncio.sleep(delay)
        if path:
            ok += 1
            if failures is not None:
                failures.note_success(url)
        else:
            failed += 1
            _log(logger, "warning", "图片下载失败（已重试 %d 次）：%s（%s）",
                 retry, url, last_err or "未取到图片内容")
        if pause:
            await asyncio.sleep(pause)               # 与抓正文一样，给门户留间隔

    if failures is not None:
        failures.save()
    return {"missing": len(candidates), "tried": len(todo), "ok": ok, "failed": failed,
            "remaining": max(0, len(candidates) - len(todo)), "skipped": skipped}


async def refresh_one_record(store, portal, nid, *, logger=None) -> dict:
    """重抓**指定**记录（`id`）的正文页，刷新正文 + 图片 + 表格。

    与批量补抓共用同一段解析逻辑，区别只是目标由调用方指定 —— 用于
    「这条通知的表格想马上出图」这类单点需求，不必等批量补抓轮到它。
    """
    rec = store.get_notice(nid)
    if not rec:
        return {"ok": False, "reason": f"未找到 #{nid}"}
    url = (rec.get("url") or "").strip()
    if not url:
        return {"ok": False, "reason": f"#{nid} 没有原文链接，无法重抓"}
    try:
        d = await portal.fetch_detail(url)
    except Exception as e:  # noqa: BLE001
        _log(logger, "warning", "刷新 #%s 失败：%s", nid, e)
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    media = [it for it in (d.get("images") or [])
             if isinstance(it, dict)
             and (it.get("url") or it.get("table") is not None)]
    tables = [str(t) for t in (d.get("tables") or [])]
    fields: dict = {"images": media, "tables": tables}
    body = (d.get("body") or "").strip()
    if body:
        fields["body"] = body
    for k in ("dept", "date"):
        if d.get(k):
            fields[k] = d[k]
    store.update_notices({rec["id"]: fields})
    imgs = sum(1 for it in media if it.get("url"))
    _log(logger, "info", "已刷新 #%s：图片 %d 张 / 表格 %d 个",
         nid, imgs, len(tables))
    return {"ok": True, "images": imgs, "tables": len(tables)}


async def recrawl_missing_images(store, portal, *, limit: int = 0, pause: float = 0.2,
                                 flush_every: int = 25, logger=None) -> dict:
    """重抓「没有 images 字段」的历史记录，补出图片位置；返回统计。

    - `limit=0` 表示本次处理全部待补记录；否则只处理最旧的 N 条（可反复调用，天然可续跑）；
    - 重抓成功后会**同步刷新正文**（图片偏移是相对新正文的，必须一起更新）；
    - 每 `flush_every` 条落一次盘，中途中断也不会丢已完成的进度。
    """
    targets = records_missing_images(store)
    todo = targets if limit <= 0 else targets[:limit]
    updated = failed = no_images = no_tables = 0
    pending: dict[int, dict] = {}

    for i, rec in enumerate(todo, 1):
        try:
            d = await portal.fetch_detail(rec["url"])
        except Exception as e:  # noqa: BLE001 — 单条失败继续
            failed += 1
            _log(logger, "warning", "补图失败（id=%s %s）：%s",
                 rec.get("id"), (rec.get("title") or "")[:20], e)
            continue
        media = [it for it in (d.get("images") or [])
                 if isinstance(it, dict)
                 and (it.get("url") or it.get("table") is not None)]
        # 表格原文必须一起存：正文文本里表格是拍平的，丢了 HTML 就渲染不出图片
        fields: dict = {"images": media,
                        "tables": [str(t) for t in (d.get("tables") or [])]}
        images = [it for it in media if it.get("url")]
        body = (d.get("body") or "").strip()
        if body:
            fields["body"] = body
        for k in ("dept", "date"):
            if d.get(k):
                fields[k] = d[k]
        pending[rec["id"]] = fields
        updated += 1
        if not images:
            no_images += 1
        if not fields["tables"]:
            no_tables += 1
        if len(pending) >= flush_every:
            store.update_notices(pending)
            pending.clear()
        if pause:
            await asyncio.sleep(pause)
        if i % 25 == 0 or i == len(todo):
            _log(logger, "info", "补图进度 %d/%d（最近：%s）", i, len(todo),
                 (rec.get("title") or "")[:24])

    if pending:
        store.update_notices(pending)
    return {"targets": len(targets), "tried": len(todo), "updated": updated,
            "no_tables": no_tables,
            "failed": failed, "no_images": no_images,
            "remaining": len(records_missing_images(store))}
