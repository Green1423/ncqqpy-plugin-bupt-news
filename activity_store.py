"""第二课堂活动的本地存储：去重、已推送标记、免打扰积压。

与通知（`store.py`）分开是有意的：活动没有正文/图片/摘要，生命周期也短
（过期即无用），放进 `notices.jsonl` 只会污染通知的 retention 与检索。
所以这里用**单独一个 JSON 文件**（`activities.json`），数据量小（几百条），
一次读、原子写。

去重与"推没推过"分开记：
* `seen` —— 见过（已入库），用于判断"新活动"；
* `pushed` —— 已成功推送，用于撑起免打扰期间的积压（睡了不丢，醒了补发）。
两者必须分开：免打扰期间活动会被标 `seen`，若用 `seen` 推积压，
这些活动就永远不会补发了。
"""
from __future__ import annotations

import json
import os
import time


class ActivityStore:
    """活动记录 + 去重/推送标记。"""

    def __init__(self, path: str, *, max_records: int = 300, logger=None):
        self.path = path
        self.max_records = max(1, int(max_records or 300))
        self.logger = logger
        self._data = {"version": 1, "seen": [], "pushed": [], "records": []}
        self.load()

    # ---------------- 读写 ----------------

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for key in ("seen", "pushed", "records"):
            value = data.get(key)
            if isinstance(value, list):
                self._data[key] = value

    def save(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.path)

    # ---------------- 记录 ----------------

    @property
    def _seen(self) -> set[str]:
        return {str(k) for k in self._data.get("seen") or []}

    @property
    def _pushed(self) -> set[str]:
        return {str(k) for k in self._data.get("pushed") or []}

    @property
    def record_count(self) -> int:
        return len(self._data.get("records") or [])

    def all_records(self) -> list[dict]:
        return [r for r in (self._data.get("records") or []) if isinstance(r, dict)]

    def upsert(self, norms: list[dict]) -> list[dict]:
        """写入本批活动，返回其中**新出现**的（按输入顺序）。

        同 id 的重复活动会刷新记录内容（活动可能改时间/改地点），
        但不会被当成新活动返回 —— 否则每轮同步都会重复推送。
        """
        known = self._seen
        by_id = {str(r.get("id")): r for r in self.all_records()
                 if r.get("id") is not None}
        fresh: list[dict] = []
        now = time.time()
        for norm in norms or []:
            if not isinstance(norm, dict):
                continue
            key = str(norm.get("id") or "")
            if not key:
                continue
            record = {k: v for k, v in norm.items() if k != "raw"}
            record["ts"] = now
            if key in by_id:
                by_id[key].update(record)      # 已有：就地刷新，不重复记 seen
                continue
            by_id[key] = record
            fresh.append(record)
            known.add(key)
        self._data["records"] = list(by_id.values())
        self._data["seen"] = sorted(known)
        self._prune()
        self.save()
        return fresh

    def backlog(self) -> list[dict]:
        """已入库但尚未推送的活动（旧 → 新）—— 免打扰期间攒下的就是它。"""
        pushed = self._pushed
        out = [r for r in self.all_records()
               if r.get("id") is not None and str(r["id"]) not in pushed]
        return out

    def mark_pushed(self, ids: list) -> None:
        keys = self._pushed | {str(i) for i in (ids or []) if i is not None}
        self._data["pushed"] = sorted(keys)
        self._prune()
        self.save()

    def is_pushed(self, act_id) -> bool:
        return str(act_id) in self._pushed

    # ---------------- 维护 ----------------

    def _prune(self) -> int:
        """只保留最近 `max_records` 条记录；已推送/已见的标记只保留还有记录的。

        返回丢掉的记录数。活动过期后没有保留价值，留太多只会让文件膨胀。
        """
        records = self.all_records()
        dropped = 0
        if len(records) > self.max_records:
            dropped = len(records) - self.max_records
            records = records[-self.max_records:]
            self._data["records"] = records
        alive = {str(r.get("id")) for r in records if r.get("id") is not None}
        for key in ("seen", "pushed"):
            values = [str(k) for k in self._data.get(key) or []]
            kept = [k for k in values if k in alive]
            if len(kept) != len(values):
                self._data[key] = sorted(set(kept))
        return dropped
