#!/usr/bin/env python
"""给存量通知补上正文图片（一次性 / 可重复执行）。

背景：`images` 字段是后加的，之前入库的记录只有正文、没有图片信息。
本脚本重抓这些记录的正文页，补出图片 URL 与位置，并把图片下载进 `data/images/` 缓存，
之后 `/buptnews get` 就能按原排版把图发出来。

⚠️ 运行前请先停掉 bot：它内存里的 store 会在下次 save 时覆盖 notices.jsonl。

用法:
    venv-arm/bin/python backfill_images.py --dry-run         # 只看有多少条要补
    venv-arm/bin/python backfill_images.py                   # 全部补齐（重抓 + 下载）
    venv-arm/bin/python backfill_images.py --limit 50        # 只补最旧的 50 条（可反复跑）
    venv-arm/bin/python backfill_images.py --download-only   # 只下载已记录但没缓存的图
    venv-arm/bin/python backfill_images.py --no-download     # 只重抓记录，不下图片
    venv-arm/bin/python backfill_images.py --no-backup       # 不备份 notices.jsonl（默认会备份）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

# 本文件在 tests/ 下 → 仓库根是上一级（脚本原来放在仓库根）。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                 # napcat 框架
sys.path.insert(0, str(ROOT / "plugins"))     # bupt_news 包（backfill/images/portal/store 用相对导入）

from napcat.config import ConfigManager
from bupt_news.backfill import (download_missing_images,             # noqa: E402
                                missing_image_urls, recrawl_missing_images,
                                records_missing_images)
from bupt_news.images import ImageCache                              # noqa: E402
from bupt_news.portal import PortalClient                            # noqa: E402
from bupt_news.store import StateStore                               # noqa: E402

DEFAULT_DATA = ROOT / "plugins" / "bupt_news" / "data"


def log(*a) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def parse_args():
    ap = argparse.ArgumentParser(description="给存量通知补正文图片")
    ap.add_argument("--limit", type=int, default=0,
                    help="本次最多重抓多少条历史记录（0=全部）")
    ap.add_argument("--download-limit", type=int, default=0,
                    help="本次最多下载多少张图片（0=全部）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不抓取不写盘")
    ap.add_argument("--download-only", action="store_true", help="跳过重抓，只下载缺失图片")
    ap.add_argument("--no-download", action="store_true", help="只重抓记录，不下载图片")
    ap.add_argument("--no-backup", action="store_true", help="不备份 notices.jsonl")
    ap.add_argument("--data-dir", default="", help="覆盖数据目录（默认取配置/插件目录）")
    return ap.parse_args()


async def main() -> int:
    args = parse_args()
    cfg = ConfigManager(str(ROOT / "config.json"))
    pcfg = cfg.get("plugins.bupt_news") or {}
    data_dir = Path(args.data_dir or pcfg.get("data_dir") or DEFAULT_DATA)
    notices = data_dir / "notices.jsonl"

    log(f"数据目录: {data_dir}")
    store = StateStore(str(data_dir / "state.json"), str(notices),
                       retention=int(pcfg.get("notice_retention", 500) or 500))
    pending_records = records_missing_images(store)
    log(f"记录总数: {store.notice_count()}；待补图（无 images 字段）: {len(pending_records)} 条")

    im = pcfg.get("images") or {}
    portal = PortalClient(
        username=os.environ.get("BUPT_PORTAL_USERNAME") or pcfg.get("username") or "",
        password=os.environ.get("BUPT_PORTAL_PASSWORD") or pcfg.get("password") or "",
        logger=None,
        cookie_file=str(data_dir / "cookies.json"),
        timeout=25,
    )
    images = ImageCache(str(data_dir / "images"), portal_getter=lambda: portal,
                        enabled=bool(im.get("enabled", True)),
                        max_bytes=int(im.get("max_bytes", 10 * 1024 * 1024) or 0))
    log(f"待下载图片: {len(missing_image_urls(store, images))} 张")

    if args.dry_run:
        await portal.close()
        log("dry-run：未抓取、未写盘")
        return 0

    if not args.no_backup and notices.exists():
        bak = notices.with_suffix(f".jsonl.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(notices, bak)
        log(f"已备份: {bak.name}")

    rc = {"targets": 0, "tried": 0, "updated": 0, "failed": 0,
          "no_images": 0, "remaining": len(pending_records)}
    if not args.download_only:
        log("开始重抓历史记录（每条一次请求，请耐心等）...")
        rc = await recrawl_missing_images(store, portal, limit=args.limit,
                                         logger=_PrintLogger())
        log(f"重抓完成：成功 {rc['updated']} 条 / 失败 {rc['failed']} 条"
            f"（其中 {rc['no_images']} 条页面本身无图）；剩余 {rc['remaining']} 条")

    dl = {"missing": 0, "tried": 0, "ok": 0, "failed": 0, "remaining": 0}
    if not args.no_download:
        log("开始下载图片...")
        dl = await download_missing_images(store, images, limit=args.download_limit)
        log(f"下载完成：成功 {dl['ok']} / 失败 {dl['failed']}；仍有 {dl['remaining']} 张待补")

    files, size = images.stats()
    log(f"缓存现状：{files} 个文件 / {size / 1048576:.1f} MB")
    await portal.close()

    ok = (not rc["failed"]) and (not dl["failed"])
    if rc["remaining"]:
        log(f"还有 {rc['remaining']} 条记录没补（用 --limit 分批跑时正常）")
    return 0 if ok else 1


class _PrintLogger:
    """把 backfill 的日志接到 stdout（脚本里不配 logging）。"""

    def _emit(self, level, msg, *a):
        try:
            text = msg % a if a else msg
        except Exception:
            text = f"{msg} {a}"
        log(f"{level.upper()}: {text}")

    def info(self, msg, *a): self._emit("info", msg, *a)
    def warning(self, msg, *a): self._emit("warn", msg, *a)
    def error(self, msg, *a): self._emit("error", msg, *a)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
