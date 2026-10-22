"""插件状态持久化：小状态（state.json）+ 可检索通知记录（notices.jsonl）。

拆两个文件的原因：500 条正文约 400 KB，若并入 state.json 则每轮同步都要全量重写，
对 SD 卡产生 ~57 MB/天写放大；notices.jsonl 只在新增时追加。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Iterable, Optional

STATE_VERSION = 2

# 非本地编号前缀：`E<treeid>-<newsid>`
EXTERNAL_PREFIX = "E"

# 置顶到达上限且没有 force 时，`pin()` 的返回值（调用方据此提示用户）
PIN_LIMIT_REACHED = "limit"


def _add_pin_entry(pins: list[dict], entry: dict, *,
                   limit: int, force: bool) -> tuple[int | None, dict | None]:
    """把 `entry` 加入置顶列表，返回 `(新编号, 被替换掉的置顶项 | None)`。

    * `limit > 0` 且已满、`force=False` → 返回 `(None, None)`（调用方提示"已满"）
    * `limit > 0` 且已满、`force=True`  → 淘汰**最早置顶**的那条（FIFO），
      新项继承它的编号（置顶区始终是紧凑的 P1…Pn）
    * 编号取最小空号：取消置顶后空出来的号会被下一次置顶复用
    """
    used = {p["n"] for p in pins if p.get("n")}
    num = 1
    while num in used:
        num += 1
    evicted: dict | None = None
    if limit and len(pins) >= limit:
        if not force:
            return None, None
        # 淘汰最早置顶的那条；FIFO 用**置顶顺序**（列表顺序）判断，
        # 不能用 P 编号 —— 编号会复用，P1 可能反而是最新加的。
        oldest_idx = 0
        if pins:
            evicted = pins.pop(oldest_idx)
            num = int(evicted.get("n") or num)
    entry = dict(entry, n=num)
    pins.append(entry)
    return num, evicted


def external_code(notice) -> str:
    """从通知（dict 或 Notice）生成可解码编号 `E<treeid>-<newsid>`。

    带前缀是为了与本地编号（纯数字）和置顶编号（`P<n>`）彻底分开。
    取不到 treeid/newsid 时返回空串（调用方据此回退成链接）。
    """
    if isinstance(notice, dict):
        treeid = str(notice.get("treeid") or "").strip()
        newsid = str(notice.get("newsid") or "").strip()
    else:
        treeid = str(getattr(notice, "treeid", "") or "").strip()
        newsid = str(getattr(notice, "newsid", "") or "").strip()
    if not treeid or not newsid:
        return ""
    # 只保留数字，避免把怪字符带进编号（门户的这两个 id 都是纯数字）
    treeid = re.sub(r"\D", "", treeid)
    newsid = re.sub(r"\D", "", newsid)
    if not treeid or not newsid:
        return ""
    return f"{EXTERNAL_PREFIX}{treeid}-{newsid}"


def decode_external(code) -> tuple[str, str] | None:
    """把 `E<treeid>-<newsid>` 解回 `(treeid, newsid)`；不是外部编号返回 None。

    编号里**自带**还原门户链接所需的两个参数，因此不需要任何映射表：
    拿到编号就能构造原文地址（`external_url()`），哪怕缓存早被清理。
    """
    text = str(code or "").strip()
    m = re.fullmatch(r"[Ee](\d{1,12})-(\d{1,20})", text)
    if not m:
        return None
    return m.group(1), m.group(2)


def external_url(code, base: str = "http://my.bupt.edu.cn") -> str:
    """非本地编号 → 门户原文链接（编号里就含 treeid/newsid）。"""
    pair = decode_external(code)
    if not pair:
        return ""
    treeid, newsid = pair
    return (f"{base}/xntz_content.jsp?urltype=news.NewsContentUrl"
            f"&wbtreeid={treeid}&wbnewsid={newsid}")


def is_external_code(code) -> bool:
    return decode_external(code) is not None


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
            # 置顶项：[{"n": 置顶编号 P<n>, "id": 本地通知 id}]
            # 置顶**有自己的独立编号**（P1/P2…，按置顶顺序递增；取消后空号可复用），
            # 与通知编号是两个空间 —— 用户 `unpin P2` 摘的是「第 2 个置顶项」，
            # 而不是 id 为 2 的通知。
            # 放在 state.json（不是 notices.jsonl）：置顶是"用户选择"这类小状态，
            # 且必须比正文记录活得久（记录被 retention 淘汰后置顶项也要能清理）。
            "pins": [],
            # 非本地（门户检索）结果的**可解码编号**缓存：
            # `{"E<treeid>-<newsid>": {"notice": {...}, "ts": 时间}}`
            # 编号本身就能还原原文链接（见 external_code / decode_external），
            # 这里只是把标题/日期等元数据顺手带上，好让 `get`/`pin` 免于重新抓取。
            "externals": {},
            # 插件自用的通用元数据（例：活动下次刷新时间、token 提醒日期）。
            # ⚠️ 必须列在这里：`load()` 只回填**已知键**，不在默认结构里的键
            # 落盘后会被静默丢弃（踩过：meta_set 写了，重载后 meta_get 拿到 None）。
            "meta": {},
        }
        self._index: dict[str, set] = {}
        self._pushed: set[str] = set()
        #: 最近一次 pin()/pin_external() 被替换掉的置顶项（给调用方提示用）
        self.last_pin_evicted: dict | None = None
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
        self._prune_pins()

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

    # ---------------- 通用元数据 ----------------

    def meta_get(self, key: str, default=None):
        """读一个插件自用的元数据（写在 state.json 的 `meta` 段）。

        给小状态用（例：某个提醒今天发过没有）。放这里而不是内存属性，
        是因为**插件热重载会重建实例**，内存态会丢，同一件事就会重复提醒。
        """
        meta = self._data.get("meta")
        if not isinstance(meta, dict):
            return default
        return meta.get(key, default)

    def meta_set(self, key: str, value) -> None:
        meta = self._data.get("meta")
        if not isinstance(meta, dict):
            meta = self._data["meta"] = {}
        meta[key] = value
        self.save()

    # ---------------- 置顶（独立编号 P<n>） ----------------

    def _pins(self) -> list[dict]:
        """规范化后的置顶列表 `[{n, id}]`（按置顶顺序，先置顶的在前）。"""
        raw = self._data.get("pins")
        if not isinstance(raw, list):
            raw = []
        out: list[dict] = []
        seen: set[int] = set()
        for item in raw:
            # 兼容旧格式：v1 直接存 [id, id, ...]
            if isinstance(item, int) or (isinstance(item, str)
                                         and item.lstrip("-").isdigit()):
                item = {"n": None, "id": int(item)}
            if not isinstance(item, dict):
                continue
            try:
                num = int(item.get("n"))
            except (TypeError, ValueError):
                num = None
            # 本地置顶：{n, id}；非本地置顶：{n, ext, notice}
            if item.get("ext"):
                code = str(item.get("ext"))
                if code in dict.fromkeys(p.get("ext") for p in out):
                    continue
                out.append({"n": num, "ext": code,
                            "notice": dict(item.get("notice") or {})})
                continue
            try:
                nid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if nid in seen:
                continue
            seen.add(nid)
            out.append({"n": num, "id": nid})
        return out

    def _assign_pin_numbers(self, pins: list[dict]) -> bool:
        """给缺编号的置顶项补号（旧数据升级用）；返回是否有改动。"""
        used = {p["n"] for p in pins if p.get("n")}
        changed = False
        for p in pins:
            if p.get("n"):
                continue
            num = 1
            while num in used:
                num += 1
            p["n"] = num
            used.add(num)
            changed = True
        return changed

    def _prune_pins(self) -> int:
        """丢掉已不存在的通知的置顶项（记录被 retention 淘汰后）。

        返回清理条数。不在这里落盘：由调用方按需 save()，避免加载路径产生写盘。
        """
        pins = self._pins()
        if self._assign_pin_numbers(pins):
            self._data["pins"] = pins
        keep = [p for p in pins if p.get("ext") or p.get("id") in self._notices]
        dropped = len(pins) - len(keep)
        if dropped:
            self._data["pins"] = keep
        return dropped

    def pinned_entries(self) -> list[dict]:
        """置顶项 `[{n, id}]`（按置顶顺序，先置顶的在前）。"""
        return self._pins()

    def pinned_ids(self, newest_first: bool = False) -> list[int]:
        """**本地**置顶的通知 id（跳过外部置顶，它没有本地 id）。

        外部置顶（门户结果）用 `pinned_entries()` 取，那里面带 `ext` 与快照。
        """
        ids = [p["id"] for p in self._pins() if isinstance(p.get("id"), int)]
        return list(reversed(ids)) if newest_first else ids

    def is_pinned(self, nid) -> bool:
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            return False
        return any(p.get("id") == nid for p in self._pins())

    def is_pinned_external(self, code) -> bool:
        """这个外部编号是否已在置顶里。"""
        code = str(code or "").strip().upper()
        return any(str(p.get("ext") or "").upper() == code for p in self._pins())

    @property
    def pin_count(self) -> int:
        return len(self._pins())

    def pin_number_of(self, nid) -> int | None:
        """通知对应的置顶编号 P<n>；没置顶返回 None。"""
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            return None
        for p in self._pins():
            if p.get("id") == nid:
                return p["n"]
        return None

    def pin_entry(self, num) -> dict | None:
        """按置顶编号 P<n> 找置顶项。"""
        try:
            num = int(str(num).strip().lstrip("Pp"))
        except (TypeError, ValueError):
            return None
        for p in self._pins():
            if p["n"] == num:
                return p
        return None

    def pin(self, nid, *, limit: int = 0, force: bool = False):
        """置顶一条通知。

        返回：分配的置顶编号（int） / `PIN_LIMIT_REACHED`（已满且没 force）/
        `None`（不存在或已置顶）。被替换掉的置顶项记在 `last_pin_evicted`。

        编号取**最小空号**：取消置顶后空出来的号会被下一次置顶复用，
        于是置顶列表始终是紧凑的 P1…Pn（不会越用越大）。
        立即落盘（置顶必须持久化）。
        """
        self.last_pin_evicted = None
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            return None
        if nid not in self._notices or self.is_pinned(nid):
            return None
        pins = self._pins()
        self._assign_pin_numbers(pins)
        num, evicted = _add_pin_entry(pins, {"id": nid},
                                      limit=int(limit or 0), force=bool(force))
        if num is None:
            self._data["pins"] = pins      # 补号这类规范化仍要留下
            return PIN_LIMIT_REACHED
        self._data["pins"] = pins
        self.last_pin_evicted = evicted
        self.save()
        return num

    def pin_external(self, notice: dict, *, limit: int = 0,
                     force: bool = False):
        """置顶一条**非本地**通知（门户结果）。

        返回值/`last_pin_evicted` 语义与 `pin()` 相同（含上限与 force 替换）。
        

        快照整条通知：门户结果不在本地库里，编号虽能解码出链接，
        但要能直接 `get P<n>` 看全文，就得把正文/图片/表格一起留着。
        已置顶返回 None。
        """
        code = external_code(notice)
        if not code or self.is_pinned_external(code):
            return None
        pins = self._pins()
        self._assign_pin_numbers(pins)
        snapshot = dict(notice)
        snapshot.setdefault("external_id", code)
        num, evicted = _add_pin_entry(
            pins, {"ext": code, "notice": snapshot},
            limit=int(limit or 0), force=bool(force))
        if num is None:
            self._data["pins"] = pins
            return PIN_LIMIT_REACHED
        self._data["pins"] = pins
        self.last_pin_evicted = evicted
        self.save()
        return num

    def update_external_notice(self, fields: dict) -> int:
        """把 `fields` 合并进该外部通知的**所有引用处**（缓存 + 外部置顶快照）。

        `fields` 必须含 `treeid`/`newsid`（或 `external_id`）以定位是哪一条。
        返回更新了几处。立即落盘（摘要这类信息不该丢）。
        """
        code = str(fields.get("external_id") or "").strip()
        if not code:
            code = external_code(fields)
        if not code:
            return 0
        patch = dict(fields)
        patch.pop("external_id", None)
        changed = 0
        # ⚠️ 必须改**原始** `_data["pins"]` 里的 dict：`_pins()` 每次调用都会
        # 新建一批 dict（规范化产物），改它的返回值为等于什么都没做。
        ext = self._externals()
        item = ext.get(code)
        if isinstance(item, dict) and isinstance(item.get("notice"), dict):
            item["notice"].update(patch)
            item["ts"] = time.time()
            changed += 1
        for entry in self._data.get("pins") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("ext") or "").upper() != code.upper():
                continue
            notice = entry.get("notice")
            if isinstance(notice, dict):
                notice.update(patch)
                changed += 1
        if changed:
            self.save()
        return changed

    def unpin(self, nid) -> bool:
        """按**通知编号**或**外部编号**取消置顶；没有置顶返回 False。立即落盘。"""
        text = str(nid or "").strip()
        if is_external_code(text):
            code = text.upper()
            pins = self._pins()
            keep = [p for p in pins
                    if str(p.get("ext") or "").upper() != code]
            if len(keep) == len(pins):
                return False
            self._data["pins"] = keep
            self.save()
            return True
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            return False
        pins = self._pins()
        keep = [p for p in pins if p.get("id") != nid]
        if len(keep) == len(pins):
            return False
        self._data["pins"] = keep
        self.save()
        return True

    def unpin_number(self, num) -> bool:
        """按**置顶编号 P<n>** 取消置顶。立即落盘。"""
        entry = self.pin_entry(num)
        if not entry:
            return False
        return self.unpin(entry["id"])

    def clear_pins(self) -> int:
        """清空所有置顶，返回清掉的条数。立即落盘。"""
        n = len(self._pins())
        if n:
            self._data["pins"] = []
            self.save()
        return n

    def prune_pins(self) -> int:
        """清理失效置顶并落盘（供插件在同步后调用）。"""
        dropped = self._prune_pins()
        if dropped:
            self.save()
        return dropped

    # ---------------- 非本地编号（门户结果，可解码） ----------------
    #
    # 编号形如 `E<treeid>-<newsid>`（例：`E1759-142679`），**从编号本身就能还原
    # 原文链接**（门户链接的 `wbtreeid`/`wbnewsid` 正是这两个值），所以编号不依赖
    # 任何映射表就能长期使用；`externals` 里只是额外缓存标题/日期，省一次抓取。
    #
    # 与本地编号（纯数字）前缀不同（E / P），两个空间不会撞号。

    def _externals(self) -> dict:
        ext = self._data.get("externals")
        if not isinstance(ext, dict):
            ext = {}
            self._data["externals"] = ext
        return ext

    def external_put(self, notice: dict) -> str:
        """登记一条非本地通知，返回它的可解码编号 `E<treeid>-<newsid>`。

        同一通知重复登记**返回同一编号**（编号由 treeid/newsid 决定，天然幂等）。
        """
        code = external_code(notice)
        if not code:
            return ""
        ext = self._externals()
        ext[code] = {"notice": dict(notice), "ts": time.time()}
        self.save()
        return code

    def external_put_many(self, notices: list) -> list[str]:
        """批量登记非本地通知，返回与入参等长的编号列表（只落一次盘）。

        搜索一页有 10 条，逐条 `external_put` 会写 10 次 state.json —— 批量入口
        把"写盘"收敛成一次。
        """
        ext = self._externals()
        out: list[str] = []
        changed = False
        now = time.time()
        for notice in notices or ():
            data = notice if isinstance(notice, dict) else (
                notice.as_dict() if hasattr(notice, "as_dict") else {})
            code = external_code(data)
            if not code:
                out.append("")
                continue
            ext[code] = {"notice": dict(data), "ts": now}
            out.append(code)
            changed = True
        if changed:
            self.save()
        return out

    def external_get(self, code) -> dict | None:
        """按 `E<treeid>-<newsid>` 取非本地通知的缓存元数据（可能没有）。"""
        item = self._externals().get(str(code or "").strip().upper())
        if not isinstance(item, dict):
            return None
        notice = item.get("notice")
        return dict(notice) if isinstance(notice, dict) else None

    def external_sweep(self, *, ttl: float = 7 * 86400,
                       max_items: int = 500) -> int:
        """回收过期的非本地元数据缓存，返回回收条数。立即落盘。

        ⚠️ 只是清掉"标题/日期"缓存；编号本身仍可用（能解码出链接），
        所以即便被回收，`get E…`/`pin E…` 依然工作（最多重新抓一次详情）。
        """
        ext = self._externals()
        if not ext:
            return 0
        now = time.time()
        drop: list[str] = []
        for code, item in ext.items():
            try:
                ts = float((item or {}).get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0
            if not ts or now - ts > ttl:
                drop.append(code)
        if len(ext) - len(drop) > max_items:
            alive = [(c, float((i or {}).get("ts") or 0))
                     for c, i in ext.items() if c not in drop]
            alive.sort(key=lambda kv: kv[1])          # 最旧的先丢
            drop += [c for c, _ in alive[:max(0, len(alive) - max_items)]]
        if not drop:
            return 0
        for code in drop:
            ext.pop(code, None)
        self.save()
        return len(drop)

    # ---------------- 检索 ----------------

    def search(self, keyword: str, *, limit: int = 10, page: int = 1,
               date: str = "") -> tuple[list[dict], int]:
        """按关键词检索**本地**通知，返回 `(当页记录, 命中总数)`（最新在前）。

        匹配范围：标题、AI 摘要、正文、发布单位、通知编号。
        `date` 非空时只保留该日期（`YYYY-MM-DD`，与记录里的 `date` 同格式）。
        关键词按**子串**匹配且不区分大小写（中文没有词边界，分词反而会漏）。
        """
        kw = (keyword or "").strip().lower()
        if not kw:
            return [], 0
        date = (date or "").strip()
        hits: list[dict] = []
        for rec in self.all_records():          # 已按 id 升序
            if date and str(rec.get("date") or "")[:10] != date:
                continue
            haystack = "\n".join(str(rec.get(k) or "") for k in
                                 ("title", "summary", "body", "dept", "newsid"))
            if kw in haystack.lower():
                hits.append(rec)
        hits.reverse()                          # 最新在前
        total = len(hits)
        size = max(1, int(limit or 10))
        start = max(0, (max(1, int(page or 1)) - 1) * size)
        return hits[start:start + size], total

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
