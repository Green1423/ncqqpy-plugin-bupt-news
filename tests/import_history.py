#!/usr/bin/env python
"""把历史通知批量导入 bupt_news 的 store（notices.jsonl），供 /buptnews get 查询。

来源是 Node 爬虫的产出（默认 ../../output/1154/notices.jsonl）。

⚠️ 导入是**静默**的：默认把这些历史记录同时标记为 `seen` 与 `pushed`，
   因此**不会触发任何推送**。否则下一次同步会把全部历史当成积压发出去。
   若确实想把历史也推出去，用 `--no-mark-pushed`（慎用）。

用法:
    venv-arm/bin/python import_history.py --dry-run
    venv-arm/bin/python import_history.py
    venv-arm/bin/python import_history.py --summarize     # 顺便生成摘要（耗 API）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 本文件在 tests/ 下 → 仓库根是上一级。
# DEFAULT_SOURCE 沿用它原本的语义（仓库根的**上一级** /output/…），
# 该默认路径在本机并不存在，实际使用请用 --source 指定。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "bupt_news"))

from store import StateStore          # noqa: E402  复用插件的状态 owner，保证 schema 一致

SEEN_SCOPE = "_all"
DEFAULT_SOURCE = ROOT.parent / "output" / "1154" / "notices.jsonl"
DEFAULT_DATA = ROOT / "plugins" / "bupt_news" / "data"


def load_source(path: Path) -> list[dict]:
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def chronological(recs: list[dict]) -> list[dict]:
    """按 (日期, newsid 数值) 升序 —— 这样分配出来的 ID 越大越新，符合直觉。"""

    def key(r: dict):
        try:
            nid = int(r.get("newsid") or 0)
        except (TypeError, ValueError):
            nid = 0
        return (r.get("date") or "", nid)

    return sorted(recs, key=key)


def backlog_count(store: StateStore) -> int:
    """尚未推送的记录数 —— 导入后应为 0，否则下次同步会发出去。"""
    n = 0
    for rec in store.all_records():
        k = rec.get("key")
        if k and not store.is_pushed(k):
            n += 1
    return n


def load_summarize_cfg(config_path: Path) -> dict:
    """读 config.json → plugins.bupt_news.summarize，与**插件同源**。

    这样 CLI 工具与 bot 用同一套 base_url / model / batch_size / max_tokens，
    不会出现「插件能摘要、工具却走了别的端点」这种分裂。
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! 读取 {config_path} 失败（{e}），改用默认值")
        return {}
    return ((data.get("plugins") or {}).get("bupt_news") or {}).get("summarize") or {}


async def gen_summaries(store: StateStore, ids: list[int], cfg: dict) -> None:
    """cfg = plugins.bupt_news.summarize。

    api_key 优先级与插件一致：环境变量 DEEPSEEK_API_KEY > config.json。
    base_url / model 等一律取自 config（不再硬编码默认值，否则会绕过自建网关）。
    """
    from summarize import Summarizer

    env_key = os.environ.get("DEEPSEEK_API_KEY", "")
    key = env_key or str(cfg.get("api_key") or "")
    if not key:
        print("  ! 未找到 DeepSeek API key，跳过摘要生成。以下两处都是空的：")
        print("      · 环境变量 DEEPSEEK_API_KEY")
        print("      · config.json 的 plugins.bupt_news.summarize.api_key")
        return

    base_url = cfg.get("base_url") or "https://api.deepseek.com"
    model = cfg.get("model") or "deepseek-flash"
    batch = max(1, int(cfg.get("batch_size", 10) or 10))
    effort = cfg.get("reasoning_effort", "none")
    print(f"  key 来源：{'环境变量 DEEPSEEK_API_KEY' if env_key else 'config.json'}")
    print(f"  端点：{base_url}   模型：{model}   每批：{batch}   "
          f"思考链：{effort or '默认(开)'}")

    recs = [store.get_notice(i) for i in ids]
    recs = [r for r in recs if r]
    if not recs:
        print("  （没有可生成摘要的记录）")
        return
    s = Summarizer(base_url=base_url, model=model, api_key=key,
                   batch_size=batch,
                   max_tokens=int(cfg.get("max_tokens", 4096) or 4096),
                   timeout=int(cfg.get("timeout", 60) or 60),
                   reasoning_effort=effort)
    print(f"  生成摘要：{len(recs)} 条 → 约 {-(-len(recs) // batch)} 次调用")
    try:
        got = await s.summarize(recs)
        updates = {}
        for r in recs:
            d = got.get(str(r.get("newsid", "")))
            if d is not None:
                # summarize() 现在返回 NoticeDigest（摘要 + 是否面向学生）
                updates[r["id"]] = {"summary": d.summary, "summary_ai": True,
                                    "for_students": bool(d.for_students),
                                    "student_ai": True}
        if updates:
            store.update_notices(updates)
        print(f"  摘要成功 {len(updates)}/{len(recs)} 条"
              + ("" if len(updates) == len(recs) else "（未成功的会保留标题截断）"))
        if len(updates) < len(recs) and s.last_error:
            # 关键：把真实原因说出来。否则只有「0/N」这种无从下手的结论。
            print(f"  ! 失败原因：{s.last_error[:400]}")
            print("    排查方向：模型名是否在服务端白名单内（HTTP 400）、")
            print("              是否触发限流（HTTP 429，看响应里的 Limit resets at）、")
            print("              网关上游是否故障（HTTP 500）。")
    finally:
        await s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA))
    ap.add_argument("--config", default=str(ROOT / "config.json"),
                    help="从这里读 summarize 配置（与插件同源）")
    ap.add_argument("--summarize", action="store_true",
                    help="顺便生成摘要（用 config 里的 base_url/model/api_key）")
    ap.add_argument("--resummarize", action="store_true",
                    help="连同已有摘要的记录一起重新生成（默认只补缺失的）")
    ap.add_argument("--no-mark-pushed", action="store_true",
                    help="不标记为已推送（危险：下次同步会把历史当积压发出）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"源文件不存在: {src}", file=sys.stderr)
        return 1
    data_dir = Path(args.data_dir)
    state_path = data_dir / "state.json"
    notices_path = data_dir / "notices.jsonl"

    recs = chronological(load_source(src))
    # retention 给大值：导入阶段不做淘汰
    store = StateStore(str(state_path), str(notices_path), retention=10 ** 6)
    existing = {r.get("key") for r in store.all_records()}

    todo: list[tuple[str, dict]] = []
    for r in recs:
        treeid = str(r.get("treeid") or "")
        newsid = str(r.get("newsid") or "")
        if not treeid or not newsid:
            continue
        k = f"{treeid}:{newsid}"
        if k in existing:
            continue
        todo.append((k, r))

    print("=== 历史通知导入 ===")
    print(f"源文件        : {src}")
    print(f"源记录        : {len(recs)}")
    print(f"store 现有    : {len(existing)}")
    print(f"待导入        : {len(todo)}")
    print(f"标记为已推送  : {not args.no_mark_pushed}")
    if recs:
        dates = sorted(r.get("date") or "" for r in recs)
        print(f"日期范围      : {dates[0]} .. {dates[-1]}")
    print()
    print("⚠️  导入前请先**停止 bot**（./run.sh 那个进程）：")
    print("    否则 bot 内存里的旧 idCounter 会在它下次 save() 时覆盖 state.json，")
    print("    造成 notices.jsonl 与 state.json 不一致。")
    print("    （StateStore 已加自愈：加载时会把 idCounter 抬到既有记录的最大 ID，")
    print("      但仍建议停掉 bot 再导入。）")
    if not todo:
        print("\n没有需要导入的新记录。")
        if not args.summarize:
            return 0
        print("（--summarize 仍会继续：为库里尚无摘要的记录生成）")
        ids: list[tuple[int, str]] = []
    else:
        ids = []

    if todo and args.dry_run:
        print("\n-- dry-run，前 5 条预览 --")
        for k, r in todo[:5]:
            print(f"   {k}  {r.get('date')}  {(r.get('title') or '')[:34]}")
        print("  …未写入")
        return 0

    for k, r in todo:
        nid = store.add_notice({
            "key": k,
            "newsid": str(r.get("newsid")),
            "treeid": str(r.get("treeid")),
            "title": r.get("title") or "",
            "dept": r.get("dept") or "",
            "date": r.get("date") or "",
            "url": r.get("url") or "",
            "body": r.get("bodyText") or "",
            "summary": "",
        })
        ids.append((nid, k))

    keys = [k for _, k in ids]
    if keys:
        store.mark_seen(SEEN_SCOPE, keys)
        if not args.no_mark_pushed:
            store.mark_pushed(keys)
    store.mark_initialized("1154")
    store.save()

    if ids:
        print(f"\n已导入 {len(ids)} 条，ID 范围 #{ids[0][0]} .. #{ids[-1][0]}")
    else:
        print("\n本次没有新增记录。")
    print(f"notices.jsonl 现有 {store.notice_count()} 条")
    print(f"seen={store.seen_count(SEEN_SCOPE)}  "
          f"pushed={len(store._data.get('pushed') or [])}")
    print(f"待推送积压     : {backlog_count(store)}  "
          f"{'✓ 不会触发推送' if backlog_count(store) == 0 else '⚠️ 下次同步会发出！'}")

    if args.summarize:
        print("\n-- 生成摘要 --")
        # 注意：对象是「**所有还没有摘要**的记录」，而不只是本次新导入的那些。
        # 否则数据已入库后再跑 --summarize 会一条都不处理（早期版本的 bug）。
        if args.resummarize:
            targets = [r["id"] for r in store.all_records()]
        else:
            targets = [r["id"] for r in store.all_records()
                       if not (r.get("summary") or "").strip()]
        if not targets:
            print("  所有记录都已有摘要，无需生成。")
        else:
            scfg = load_summarize_cfg(Path(args.config))
            if not scfg.get("enabled", True):
                print("  ! config 里 summarize.enabled = false，跳过")
            else:
                print(f"  待生成摘要 {len(targets)} 条"
                      f"（库里共 {store.notice_count()} 条）")
                asyncio.run(gen_summaries(store, targets, scfg))
                store.save()
                done = len([r for r in store.all_records()
                            if (r.get("summary") or "").strip()])
                print(f"  回填后：{done}/{store.notice_count()} 条有摘要")

    # 抽样展示（用库里的记录，不能用 ids —— 没有新导入时 ids 是空的）
    print("\n-- 抽样（可用 /buptnews get <ID> 查询）--")
    all_ids = sorted(r["id"] for r in store.all_records())
    if not all_ids:
        print("   （库里还没有记录）")
        return 0
    for idx in (all_ids[0], all_ids[len(all_ids) // 2], all_ids[-1]):
        rec = store.get_notice(idx)
        if rec:
            print(f"   #{idx}  {rec.get('date')}  {rec.get('dept')}  "
                  f"{(rec.get('title') or '')[:30]}  [正文 {len(rec.get('body') or '')} 字]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
