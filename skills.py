"""把 bupt_news 的**只读**能力注册成 `ai_chat` 技能（见 `napcat.skills`）。

为什么值得注册：群里有人问「最近有什么通知」「有没有关于疫苗的通知」时，
AI 光凭自己的知识答不了 —— 它需要**这个插件的数据源**。注册成技能后模型能直接查，
不用用户先敲 `/buptnews search` 再把结果贴给它。

只给只读能力：推送目标、token、置顶这些有副作用或涉及权限的，继续留给命令。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

#: 详情里正文的截断长度（模型不需要整篇，太长反而挤掉上下文）
BODY_MAX = 800
#: 搜索结果里摘要的截断长度
SUMMARY_MAX = 40


def _brief(rec: dict) -> str:
    """一条通知的一行摘要（编号 + 日期 + 摘要或标题）。"""
    summary = ""
    if rec.get("summary_ai") and rec.get("summary"):
        summary = str(rec["summary"])[:SUMMARY_MAX]
    title = str(rec.get("title") or "（无标题）")
    date = str(rec.get("date") or "")
    text = f"[{summary}] {title}" if summary else title
    return f"#{rec.get('id')} {date} {text}".strip()


def _allowed(plugin, ctx: Any) -> bool:
    """技能调用也要过**服务范围** —— 绕开命令不等于绕开白名单。

    这里没有 `event`，所以按 `check_access` 的同一套判据重算：
    群聊看群号、私聊看 QQ 号，超级用户豁免。
    """
    uid = int(getattr(ctx, "user_id", 0) or 0)
    gid = int(getattr(ctx, "group_id", 0) or 0)
    if uid and plugin.perm.is_superuser(uid):
        return True
    if getattr(ctx, "is_group", False):
        return gid in plugin._bound_groups()
    return uid in plugin._bound_users()


def _is_super(plugin, ctx: Any) -> bool:
    uid = int(getattr(ctx, "user_id", 0) or 0)
    return bool(uid and plugin.perm.is_superuser(uid))


def register(plugin) -> int:
    """注册全部技能，返回注册个数（调用方只用来打日志）。"""
    count = 0

    @plugin.register_skill(
        "search",
        description=("在「北邮校内通知」本地库里检索（标题/摘要/正文/单位）。"
                     "用户问「有没有关于某某的通知」「最近那个 xx 通知说了什么」时用它。"
                     "返回若干条：编号、日期、摘要。要全文再用 notice 技能。"),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "关键词，如「疫苗」「退补选」「四六级」"},
                "limit": {"type": "integer",
                          "description": "最多几条，默认 8、上限 20"},
            },
            "required": ["query"],
        },
    )
    async def _skill_search(args: dict, ctx):
        if not _allowed(plugin, ctx):
            return "校内通知库没有对当前用户/群开放（需要管理员配置绑定）"
        query = str(args.get("query") or "").strip()
        if not query:
            return "没有给关键词"
        try:
            limit = max(1, min(int(args.get("limit") or 8), 20))
        except (TypeError, ValueError):
            limit = 8
        hits, total = plugin._store.search(query, limit=limit, page=1)
        if not hits:
            return f"本地通知库里没有搜到「{query}」（只保留最近的通知，更早的没入库）"
        lines = [f"共 {total} 条命中，列出前 {len(hits)} 条："]
        lines += [_brief(r) for r in hits]
        lines.append("要看全文，用 notice 技能并传入编号（如 476）。")
        return "\n".join(lines)

    count += 1

    @plugin.register_skill(
        "recent",
        description=("列出最近入库的校内通知（新→旧）。"
                     "用户问「最近有什么通知」「这两天学校发了什么」时用它。"),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "最多几条，默认 10、上限 30"},
            },
        },
    )
    async def _skill_recent(args: dict, ctx):
        if not _allowed(plugin, ctx):
            return "校内通知库没有对当前用户/群开放（需要管理员配置绑定）"
        try:
            limit = max(1, min(int(args.get("limit") or 10), 30))
        except (TypeError, ValueError):
            limit = 10
        ids = list(reversed(plugin._store.recent_ids(limit)))
        if not ids:
            return "本地还没有任何通知记录（可能还没同步过）"
        lines = [f"最近 {len(ids)} 条通知："]
        for nid in ids:
            rec = plugin._store.get_notice(nid)
            if rec:
                lines.append(_brief(rec))
        return "\n".join(lines)

    count += 1

    @plugin.register_skill(
        "notice",
        description=("按编号取一条校内通知的详情（部门、日期、摘要、正文摘录、原文链接）。"
                     "需要具体内容时用它；正文过长会截断。"),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string",
                       "description": "通知编号，如 476（可带 #）"},
            },
            "required": ["id"],
        },
    )
    async def _skill_notice(args: dict, ctx):
        if not _allowed(plugin, ctx):
            return "校内通知库没有对当前用户/群开放（需要管理员配置绑定）"
        raw = str(args.get("id") or "").strip().lstrip("#＃")
        try:
            nid = int(raw)
        except (TypeError, ValueError):
            return f"编号要写成数字（收到 {raw!r}）"
        rec = plugin._store.get_notice(nid)
        if not rec:
            kept = plugin.config.get("notice_retention", 500)
            return f"没有编号 {nid} 的通知（本地只保留最近 {kept} 条）"
        body = str(rec.get("body") or "").strip()
        if len(body) > BODY_MAX:
            body = body[:BODY_MAX] + "…（已截断）"
        parts = [f"#{nid} {rec.get('title') or '（无标题）'}"]
        meta = " · ".join(x for x in (rec.get("dept"), rec.get("date")) if x)
        if meta:
            parts.append(meta)
        if rec.get("summary_ai") and rec.get("summary"):
            parts.append(f"摘要：{rec['summary']}")
        if rec.get("for_students") is False:
            parts.append("（AI 判断这条可能不是发给学生的）")
        if body:
            parts.append("正文：\n" + body)
        if rec.get("url"):
            parts.append(f"原文：{rec['url']}")
        return "\n".join(parts)

    count += 1

    @plugin.register_skill(
        "activities",
        description=("列出已同步的「第二课堂」活动（按活动时间排序）。"
                     "仅超级用户可用 —— 这些活动来自某个同学的账号，不是公开信息。"),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "最多几条，默认 10、上限 30"},
                "include_past": {"type": "boolean",
                                 "description": "是否包含已结束的活动，默认 false"},
            },
        },
    )
    async def _skill_activities(args: dict, ctx):
        if not _is_super(plugin, ctx):
            # 活动的数据源是绑定账号，等同个人数据，只给超管
            return "第二课堂活动只对超级用户开放"
        bindings = getattr(plugin, "_act_bindings", None) or []
        if not bindings:
            return "还没有启用活动同步的绑定（需要 add … event=on 并配置 token）"
        try:
            limit = max(1, min(int(args.get("limit") or 10), 30))
        except (TypeError, ValueError):
            limit = 10
        include_past = bool(args.get("include_past"))
        recs = [r for b in bindings for r in b["store"].all_records()]
        if not include_past:
            today = datetime.now().strftime("%Y-%m-%d")
            recs = [r for r in recs
                    if plugin._activity_when(r) >= today]
        if not recs:
            return "没有符合条件的活动（可能都结束了，或还没同步过）"
        ordered = plugin._sort_activities(recs)[:limit]
        lines = [f"活动 {len(ordered)} 条（按活动时间）："]
        for r in ordered:
            when = plugin._activity_when(r) or "时间待定"
            place = f" · {r['place']}" if r.get("place") else ""
            lines.append(f"{r.get('id')} {when} {r.get('title') or '（无标题）'}{place}")
        return "\n".join(lines)

    count += 1

    return count
