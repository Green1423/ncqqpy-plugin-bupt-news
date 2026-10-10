"""插件状态持久化：小状态（state.json）+ 可检索通知记录（notices.jsonl）。

拆两个文件的原因：500 条正文约 400 KB，若并入 state.json 则每轮同步都要全量重写，
对 SD 卡产生 ~57 MB/天写放大；notices.jsonl 只在新增时追加。
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterable, Optional

STATE_VERSION = 2


class StateStore:
    """state.json 存小状态；notices.jsonl 追加存通知记录（含正文，供按 ID 查询）。"""

    def __init__(self, state_path: str, notices_path: Optional[str] = None,
                 max_seen: int = 3000, retention: int = 500) -> None:
        self.state_path = state_path
        self.notices_path = notices_path or os.path.join(
            os.path.dirname(state_path), "notices.jsonl")
        self.max_seen = max_seen
        self.retention = max(1, int(retention))
        self._data: dict = {
            "version": STATE_VERSION,
            "idCounter": 0,
            "seen": {},           # {column: [key, ...]} 最近在后
            "pushed": [],         # [key, ...] 已成功推送过的键（与 seen 区分）
            "initialized": {},    # {column: true} 首次同步已完成
            "lastSync": None,
            "lastDigestDate": None,
            "pushedCount": 0,     # 累计推送条数（统计用）
        }
        self._index: dict[str, set] = {}
        self._pushed: set[str] = set()
        self._notices: dict[int, dict] = {}
        self.load()

    # ---------------- 磁盘 ----------------

    def load(self) -> None:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = {}
            if isinstance(data, dict):
                # v1 -> v2 兼容：缺的键用默认值补齐（seen/initialized/lastSync 语义不变）
                for k in self._data:
                    if k in data:
                        self._data[k] = data[k]
                # ⚠️ v1 的 `pushed` 是「累计推送条数」(int)，v2 的 `pushed` 是
                # 「已推送键集合」(list) —— 同名不同义。这里显式迁移，避免把计数丢掉。
                old = data.get("pushed")
                if isinstance(old, int) and not isinstance(old, bool):
                    self._data["pushedCount"] = old
                    self._data["pushed"] = []
                if not isinstance(self._data["seen"], dict):
                    self._data["seen"] = {}
                if not isinstance(self._data["initialized"], dict):
                    self._data["initialized"] = {}
                if not isinstance(self._data["pushed"], list):
                    self._data["pushed"] = []
                try:
                    self._data["idCounter"] = int(self._data["idCounter"] or 0)
                except (TypeError, ValueError):
                    self._data["idCounter"] = 0
                try:
                    self._data["pushedCount"] = int(self._data["pushedCount"] or 0)
                except (TypeError, ValueError):
                    self._data["pushedCount"] = 0
                self._data["version"] = STATE_VERSION
        self._rebuild_index()
        self._load_notices()
        self._reconcile_id_counter()

    def save(self) -> None:
        d = os.path.dirname(self.state_path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.state_path)

    def _rebuild_index(self) -> None:
        self._index = {str(c): set(v) for c, v in self._data["seen"].items()}
        self._pushed = set(self._data.get("pushed") or [])

    def _reconcile_id_counter(self) -> None:
        """自愈：让 idCounter 不小于既有记录的最大 ID。

        会真实出现不一致的场景：**bot 正在运行期间，又从外部导入了历史通知**。
        此时 bot 内存里是旧的 idCounter（例如 0），它下次 save() 会把 state.json
        覆盖回去，而 notices.jsonl 已经有 415 条记录 —— 之后 add_notice() 会从 1
        开始分配，**与既有记录 ID 撞车**，导致 get 查到错误的内容。

        因此每次加载都以「记录里的最大 ID」兜底，只增不减。
        """
        if not self._notices:
            return
        try:
            highest = max(self._notices)
        except (TypeError, ValueError):
            return
        try:
            current = int(self._data.get("idCounter") or 0)
        except (TypeError, ValueError):
            current = 0
        if current < highest:
            self._data["idCounter"] = highest

    # ---------------- 通知记录（notices.jsonl） ----------------

    def _load_notices(self) -> None:
        self._notices = {}
        if not os.path.exists(self.notices_path):
            return
        try:
            with open(self.notices_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        self._notices[int(rec["id"])] = rec
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue        # 容忍被截断的尾行
        except OSError:
            self._notices = {}

    def _append_notice(self, rec: dict) -> None:
        d = os.path.dirname(self.notices_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.notices_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _rewrite_notices(self, keep: Optional[list[int]] = None) -> None:
        keep = sorted(self._notices) if keep is None else sorted(keep)
        tmp = self.notices_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for i in keep:
                f.write(json.dumps(self._notices[i], ensure_ascii=False,
                                   separators=(",", ":")) + "\n")
        os.replace(tmp, self.notices_path)

    def _compact_notices(self) -> None:
        """超过 retention×1.2 时重写文件，丢弃最旧记录。"""
        if len(self._notices) <= int(self.retention * 1.2):
            return
        keep = sorted(self._notices)[-self.retention:]
        self._rewrite_notices(keep)
        self._notices = {i: self._notices[i] for i in keep}

    # ---------------- 通知记录 API ----------------

    def next_id(self) -> int:
        self._data["idCounter"] = int(self._data["idCounter"]) + 1
        return self._data["idCounter"]

    def add_notice(self, rec: dict) -> int:
        """写入一条通知记录并返回其 id。"""
        nid = self.next_id()
        rec = dict(rec, id=nid, ts=rec.get("ts") or time.time())
        self._notices[nid] = rec
        self._append_notice(rec)
        self._compact_notices()
        return nid

    def get_notice(self, nid) -> Optional[dict]:
        try:
            return self._notices.get(int(nid))
        except (TypeError, ValueError):
            return None

    def recent_ids(self, limit: int = 20) -> list[int]:
        return sorted(self._notices)[-limit:]

    def all_records(self) -> list[dict]:
        """全部通知记录，按 id 升序（最旧在前）。"""
        return [self._notices[i] for i in sorted(self._notices)]

    def update_notice(self, nid, **fields) -> None:
        """更新单条已写记录（摘要回填用）。"""
        self.update_notices({nid: fields})

    def update_notices(self, updates: dict) -> None:
        """批量更新 {id: {field: value}}，只重写一次文件。"""
        changed = False
        for nid, fields in (updates or {}).items():
            try:
                rec = self._notices.get(int(nid))
            except (TypeError, ValueError):
                continue
            if rec:
                rec.update(fields)
                changed = True
        if changed:
            self._rewrite_notices()

    def notice_count(self) -> int:
        return len(self._notices)

    # ---------------- seen / pushed ----------------

    def is_seen(self, column, key: str) -> bool:
        return key in self._index.get(str(column), set())

    def mark_seen(self, column, keys: Iterable[str]) -> int:
        col = str(column)
        ids = self._data["seen"].setdefault(col, [])
        idx = self._index.setdefault(col, set())
        added = 0
        for k in keys:
            if k not in idx:
                idx.add(k)
                ids.append(k)
                added += 1
        if len(ids) > self.max_seen:
            drop = len(ids) - self.max_seen
            removed = ids[:drop]
            del ids[:drop]
            idx.difference_update(removed)
        return added

    def is_pushed(self, key: str) -> bool:
        return key in self._pushed

    def mark_pushed(self, keys: Iterable[str]) -> None:
        for k in keys:
            if k not in self._pushed:
                self._pushed.add(k)
                self._data["pushed"].append(k)
        if len(self._data["pushed"]) > self.max_seen:
            keep = self._data["pushed"][-self.max_seen:]
            self._data["pushed"] = keep
            self._pushed = set(keep)

    # ---------------- 其他状态 ----------------

    def is_initialized(self, column) -> bool:
        return bool(self._data["initialized"].get(str(column)))

    def mark_initialized(self, column) -> None:
        self._data["initialized"][str(column)] = True

    def seen_count(self, column) -> int:
        return len(self._data["seen"].get(str(column), []))

    @property
    def pushed_count(self) -> int:
        return int(self._data.get("pushedCount") or 0)

    @property
    def last_sync(self) -> Optional[float]:
        return self._data.get("lastSync")

    @property
    def last_digest_date(self) -> Optional[str]:
        return self._data.get("lastDigestDate")

    def note_sync(self) -> None:
        self._data["lastSync"] = time.time()

    def note_digest(self, day: str) -> None:
        self._data["lastDigestDate"] = day

    def note_push(self, count: int) -> None:
        self._data["pushedCount"] = self.pushed_count + max(0, int(count))
