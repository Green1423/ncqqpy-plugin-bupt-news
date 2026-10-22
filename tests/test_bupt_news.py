#!/usr/bin/env python
"""bupt_news 插件自检脚本。

默认离线运行（解析 / 状态 / 加载 / 同步逻辑 / 命令分发 / WS 重连与上报节流，
全部用夹具、桩件与本地回环 WebSocket 服务）。
加 --online 会真实登录门户，验证网络链路。

用法:
    venv-arm/bin/python test_bupt_news.py
    BUPT_PORTAL_PASSWORD=xxx venv-arm/bin/python test_bupt_news.py --online
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import shutil
import socket
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import websockets

# 本文件在 tests/ 下 → 仓库根是上一级（脚本原来放在仓库根）。
# TMP/夹具目录落在 tests/.test-tmp/ 下，与其它测试一致。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)   # 新 clone 里没有它（.gitignore 排除）

# ---- 插件包位置：两种仓库布局都要能跑 --------------------------------------
# 框架仓库布局 = <repo>/plugins/bupt_news/（直接可用）；
# 独立插件仓库布局 = 仓库根就是包目录，而且 clone 出来的目录名未必等于插件名。
# 后者搭一个「包农场」<tmp>/pkgfarm/plugins/bupt_news -> 插件包目录，让
# `import plugins.bupt_news.*` 与 `plugin_dir=<农场>/plugins` 两种口径都成立。
PLUGIN_NAME = "bupt_news"
PLUGIN_DIR = ROOT / "plugins" / PLUGIN_NAME
PLUGIN_ROOT_DIR = ROOT / "plugins"
if not (PLUGIN_DIR / "__init__.py").exists() and (ROOT / "__init__.py").exists():
    import tempfile
    PLUGIN_ROOT_DIR = (Path(tempfile.mkdtemp(prefix=f"pkgfarm-{PLUGIN_NAME}-"))
                       / "plugins")
    PLUGIN_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    if not (PLUGIN_ROOT_DIR / PLUGIN_NAME).exists():
        (PLUGIN_ROOT_DIR / PLUGIN_NAME).symlink_to(ROOT, target_is_directory=True)
    sys.path.insert(0, str(PLUGIN_ROOT_DIR.parent))
    PLUGIN_DIR = PLUGIN_ROOT_DIR / PLUGIN_NAME
PLUGIN_PKG = f"plugins.{PLUGIN_NAME}"

from napcat.config import ConfigManager
from napcat.client import GroupBinding
from napcat.models import GroupIncreaseEvent, MessageEvent
from napcat.message import MessageChain
from napcat.command import CommandManager                              # noqa: E402
from napcat.events import EventBus, TimerManager                       # noqa: E402
from napcat.file_manager import FileManager                            # noqa: E402
from napcat.permission import PermissionManager                        # noqa: E402
from napcat.plugin import PluginManager                                # noqa: E402

SUPERUSER = 10000001
# 夹具的私聊可用范围：群聊临时会话只放行这一个群，直接私聊只放行这一个 QQ。
# 两个都变成常量，是因为「哪个群/哪个人在范围内」是这一节测试的主角 ——
# 散落的字面量会让「这条断言测的是范围还是权限」变得看不出来。
GROUP_BOUND = 1000000001
USER_ALLOWED = 20001
# 自己的子目录：清空时别把别的套件的临时目录一起删掉（并行跑会互相打断）
TMP = ROOT / "tests" / ".test-tmp" / "bupt_news"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASS.append(name)
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        FAIL.append(name)
        print(f"  \033[31mFAIL\033[0m {name}" + (f"  <- {detail}" if detail else ""))
    return bool(cond)


# ---------------------------------------------------------------- 夹具

LIST_FIXTURE = """<!DOCTYPE html><html><body>
<ul class="newslist list-unstyled">
<li> <a href="xntz_content.jsp?urltype=news.NewsContentUrl&amp;wbtreeid=1158&amp;wbnewsid=138177"
       target="_blank" title="关于印发《北京邮电大学2026年工作要点》及任务分解的通知" style="">关于印发《北京邮电大学2026年工作要点》及任务分解的通知</a>
     <span class="author">党政办公室</span> <span class="time">2026-03-28</span> </li>
<li> <a href="xntz_content.jsp?urltype=news.NewsContentUrl&amp;wbtreeid=2149&amp;wbnewsid=142431"
       target="_blank" title="关于2026年9月批次授予普通全日制学生学士学位的通知" style="">关于2026年9月批次授予普通全日制学生学士学位的通知</a>
     <span class="author">教务部</span> <span class="time">2026-09-23</span> </li>
</ul>
<a href="?totalpage=1601&amp;PAGENUM=2&amp;urltype=tree.TreeTempUrl&amp;wbtreeid=1154">2</a>
<a href="?totalpage=1601&amp;PAGENUM=1601&amp;urltype=tree.TreeTempUrl&wbtreeid=1154">尾页</a>
</body></html>"""

DETAIL_FIXTURE = """<!DOCTYPE html><html><head>
<title>关于开展某某工作的通知-欢迎访问信息服务门户</title></head><body>
<h1>关于开展某某工作的通知</h1>
<div class="pmeta">发布部门：教务部 发布时间： 2026-09-23</div>
<div class="content"><div id="vsb_content">
  <p>各学院：</p>
  <p>根据学校安排，现将有关事项通知如下：</p>
  <p>一、报名时间<br>2026年9月24日至9月30日。</p>
  <ul><li>附件1</li><li>附件2</li></ul>
</div></div>
<script>var x = "<p>不应被抓取</p>";</script>
<div class="battch"><a href="/system/_content/download.jsp?owner=1&wbfileid=2">表格.xlsx</a></div>
</body></html>"""


DETAIL_IMAGES_FIXTURE = """<!DOCTYPE html><html><head>
<title>关于开展图片排版测试的通知-欢迎访问信息服务门户</title></head><body>
<h1>关于开展图片排版测试的通知</h1>
<div class="pmeta">发布部门：教务部 发布时间： 2026-09-24</div>
<div id="vsb_content">
  <p>第一段文字，介绍背景。</p>
  <p><img src="/system/_content/download.jsp?wbfileid=1" alt="图一"></p>
  <p>第二段文字，说明流程。</p>
  <p><img data-src="images/pic2.png" /></p>
  <p>第三段收尾。</p>
</div>
<div class="battch"><a href="/system/_content/download.jsp?owner=1&wbfileid=2">表格.xlsx</a></div>
</body></html>"""


def make_notice(Notice, newsid: str, title: str, treeid: str = "2149"):
    return Notice(
        column="1154", newsid=newsid, treeid=treeid, title=title,
        dept="教务部", date="2026-09-23",
        url=f"http://my.bupt.edu.cn/xntz_content.jsp?wbtreeid={treeid}&wbnewsid={newsid}",
    )


class FakeApi:
    """记录发送内容，不联网。

    `sent` 保持 `(kind, target, message)` 三元组；群聊临时会话（小窗）回复额外把
    来源群号记在 `temp_sessions` 里：`[(user_id, group_id), …]`。
    `deleted` 记录撤回过的 `message_id`（`message_id` = 本条发送在 `sent` 里的下标 + 1）。
    """

    def __init__(self):
        self.sent: list[tuple] = []
        self.temp_sessions: list[tuple] = []
        self.deleted: list[int] = []

    async def send_group_msg(self, group_id, message, auto_escape=False):
        self.sent.append(("group", str(group_id), message))
        return {"message_id": len(self.sent)}

    async def send_private_msg(self, user_id, message, auto_escape=False, group_id=None):
        self.sent.append(("private", str(user_id), message))
        if group_id:                     # 非空 = 群聊临时会话回复
            self.temp_sessions.append((str(user_id), str(group_id)))
        return {"message_id": len(self.sent)}

    async def delete_msg(self, message_id: int) -> None:
        self.deleted.append(int(message_id))

    def texts(self):
        out = []
        for kind, target, msg in self.sent:
            segs = msg.build() if hasattr(msg, "build") else msg
            out.append((kind, target, "".join(
                s.get("data", {}).get("text", "") for s in segs if s.get("type") == "text"
            )))
        return out


class FakePortal:
    def __init__(self, notices, body="正文内容", images=None,
                 image_bytes=b"\xff\xd8\xff\xe0FAKE-JPEG-BYTES",
                 image_ctype="image/jpeg"):
        self.notices = notices
        self.body = body
        self.images = list(images or [])      # [{"pos": int, "url": str}]
        self.image_bytes = image_bytes
        self.image_ctype = image_ctype
        self.username = ""
        self.password = ""
        self.list_calls = 0
        self.detail_calls = 0
        self.image_calls: list[str] = []
        self.image_fail: set[str] = set()

    async def fetch_list(self, column, pages=1):
        self.list_calls += 1
        return list(self.notices)

    async def fetch_detail(self, url):
        self.detail_calls += 1
        return {"title": "t", "dept": "教务部", "date": "2026-09-23",
                "body": self.body, "images": [dict(i) for i in self.images]}

    async def fetch_bytes(self, url):
        self.image_calls.append(url)
        if url in self.image_fail:
            raise RuntimeError("模拟图片下载失败")
        return self.image_bytes, self.image_ctype

    async def close(self):
        pass


class FakeWeather:
    """确定性天气桩，避免离线套件联网。

    返回结构化的 Weather 对象（与真实 WeatherClient 一致），`desc=None` 表示取不到天气。
    """

    def __init__(self, desc="雷阵雨 18~27℃ 降水100%", umbrella=True,
                 code=95, pop=100):
        self.desc = desc
        self.umbrella = umbrella
        self.code = code
        self.pop = pop
        self.calls = 0

    async def today(self):
        self.calls += 1
        if self.desc is None:
            return None
        W = sys.modules["napcat_plugin_bupt_news.weather"]
        return W.Weather(code=self.code, pop=self.pop, text=self.desc,
                         umbrella=self.umbrella)

    async def close(self):
        pass


class FakeSummarizer:
    """确定性摘要桩：默认返回 `摘要<newsid>`，可配置映射 / 抛错。

    需与真实 Summarizer 一样同时接受对象与记录 dict（推送走的是 dict 记录）。
    """

    def __init__(self, mapping=None, fail=False):
        self.mapping = mapping or {}
        self.fail = fail
        self.calls = 0

    @property
    def enabled(self):
        return True

    last_error = ""            # 与真实 Summarizer 同形（插件会读它做日志）

    async def summarize(self, notices):
        self.calls += 1
        if self.fail:
            self.last_error = "RuntimeError: summarizer boom"
            raise RuntimeError("summarizer boom")

        def pair(n):
            if isinstance(n, dict):
                return str(n.get("newsid", "")), str(n.get("title", ""))
            return str(n.newsid), n.title

        from napcat_plugin_bupt_news.summarize import NoticeDigest
        out = {}
        for n in notices:
            nid, _title = pair(n)
            if nid:
                # 返回 NoticeDigest（含学生判定），与真实 Summarizer 同形
                out[nid] = NoticeDigest(self.mapping.get(nid, f"摘要{nid}"), True)
        return out

    async def close(self):
        pass


def build_harness(cfg_path: Path, api, data_dir=None):
    cfg = ConfigManager(str(cfg_path))
    # 关键：在插件 on_load 之前把数据目录指向临时目录，
    # 否则 on_load 会在真实 plugins/bupt_news/data/ 上建 StateStore，
    # 定时同步一旦触发就会污染真实数据（曾导致 state.json 与 notices.jsonl 不一致）。
    if data_dir is not None:
        cfg.set("plugins.bupt_news.data_dir", str(data_dir))
    # 私聊可用范围白名单（见插件 check_access）：夹具里**显式**配，不吃默认值 ——
    # 默认是空 = 非超级用户私聊一律不服务，会让下面每个用普通用户的用例都变成
    # 「被范围挡下」：权限维度的断言照样"通过"，实际却什么都没测到。
    cfg.set("plugins.bupt_news.binding.groups", [GROUP_BOUND])
    cfg.set("plugins.bupt_news.binding.users", [USER_ALLOWED])
    bus = EventBus()
    timer = TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[SUPERUSER])
    groups = GroupBinding(bound=set(), all_groups=True)
    cmds = CommandManager(prefixes=["/", "#"], perm_manager=perm,
                          groups_ref=groups, cfg_ref=cfg,
                          event_bus_ref=bus, timer_ref=timer)
    files = FileManager(api)
    mgr = PluginManager(plugin_dir=str(PLUGIN_ROOT_DIR), api=api, event_bus=bus,
                        timer=timer, cmd_manager=cmds, perm_manager=perm,
                        file_manager=files, cfg=cfg, groups=groups)
    cmds._plugins_ref = mgr
    return mgr, cmds, timer


# ---------------------------------------------------------------- 测试

def _push(plugin, *, groups=None, users=None):
    """把推送目标写进 `targets`（新模型唯一来源）。

    传 None 表示"这一侧不动"；传了就按它精确设 `message` 开关，
    并保留已有绑定（绑定 = 准入，删了会影响访问用例）。
    """
    data = plugin._targets()
    for key, want in (("groups", groups), ("users", users)):
        if want is None:
            continue
        ids = {int(x) for x in want}
        cur = data[key]
        for tid, spec in cur.items():
            spec["message"] = tid in ids
        for tid in ids - set(cur):
            cur[tid] = {"message": True, "event": False, "token_file": ""}
    plugin.config.set("targets", {
        "groups": {str(t): dict(v) for t, v in data["groups"].items()},
        "users": {str(t): dict(v) for t, v in data["users"].items()},
    })


async def main(online: bool) -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    api = FakeApi()
    mgr, cmds, timer = build_harness(TMP / "config.json", api,
                                     data_dir=TMP / "data")

    print("\n[1] 加载插件（走真实 PluginManager）")
    await mgr.load_all()
    await mgr.enable_all()
    plugin = mgr.get("bupt_news")
    check("插件被发现并加载", plugin is not None, "PluginManager 未找到 bupt_news")
    if plugin is None:
        return 1
    check("name/version 正确", plugin.name == "bupt_news" and plugin.version == "1.0.0")
    img_cfg_default = plugin.config.get("images") or {}
    check("images 默认带失败重试配置（retry/retry_delay/max_attempts）",
          img_cfg_default.get("retry") == 2
          and float(img_cfg_default.get("retry_delay", 0)) == 2.0
          and img_cfg_default.get("max_attempts") == 5, str(img_cfg_default))

    pmod = sys.modules.get("napcat_plugin_bupt_news.portal")
    check("插件数据目录被 data_dir 配置接管（不碰真实数据）",
          str(TMP / "data") in str(plugin._data_dir), str(plugin._data_dir))
    check("portal 子模块可导入（包结构 OK）", pmod is not None)
    if pmod is None:
        return 1
    Notice = pmod.Notice

    # 隔离测试数据目录，避免污染真实 state.json
    from napcat_plugin_bupt_news.store import StateStore  # type: ignore
    plugin._data_dir = str(TMP / "data")
    os.makedirs(plugin._data_dir, exist_ok=True)
    plugin._store = StateStore(str(TMP / "data" / "state.json"))

    print("\n[2] HTML 解析（夹具）")
    items = pmod.parse_list(LIST_FIXTURE, "1154")
    check("列表解析出 2 条", len(items) == 2, f"得到 {len(items)}")
    if items:
        n0 = items[0]
        check("标题取自 title 属性", n0.title.startswith("关于印发《北京邮电大学2026年工作要点》"))
        check("部门解析正确", n0.dept == "党政办公室", n0.dept)
        check("日期解析正确", n0.date == "2026-03-28", n0.date)
        check("newsid / treeid 解析正确", n0.newsid == "138177" and n0.treeid == "1158")
        check("URL 为绝对地址", n0.url.startswith("http://my.bupt.edu.cn/"))
        check("去重键格式 treeid:newsid", n0.key == "1158:138177", n0.key)
    check("分页总页数解析 = 1601", pmod.parse_total_pages(LIST_FIXTURE) == 1601)

    d = pmod.parse_detail(DETAIL_FIXTURE)
    check("详情标题解析", d["title"] == "关于开展某某工作的通知", d["title"])
    check("详情部门解析", d["dept"] == "教务部", d["dept"])
    check("详情日期解析", d["date"] == "2026-09-23", d["date"])
    check("正文包含段落文本", "各学院" in d["body"] and "报名时间" in d["body"])
    check("<br> 转为换行", "2026年9月24日" in d["body"])
    check("正文不含 script 内容", "不应被抓取" not in d["body"])
    check("正文不含附件区/无关区块", "表格.xlsx" not in d["body"])

    print("\n[2.5] 命令解析：引号与转义")
    from napcat.command import CommandParser

    _cp = CommandParser(prefixes=["/"])

    def pargs(text):
        pkt = _cp.parse(text)
        return pkt.args if pkt else None

    check("双引号保留空格", pargs('/c "a b" c') == ["a b", "c"], str(pargs('/c "a b" c')))
    check("单引号保留空格", pargs("/c 'a b' c") == ["a b", "c"], str(pargs("/c 'a b' c")))
    check("单引号内转义单引号",
          pargs("/c 'don\\'t'") == ["don't"], str(pargs("/c 'don\\'t'")))
    check("未加引号的转义单引号",
          pargs("/c don\\'t stop") == ["don't", "stop"], str(pargs("/c don\\'t stop")))
    check("双引号内含单引号", pargs("/c \"it's\"") == ["it's"], str(pargs("/c \"it's\"")))
    check("双引号内转义双引号",
          pargs('/c "say \\"hi\\""') == ['say "hi"'], str(pargs('/c "say \\"hi\\""')))
    check("转义单引号与反斜杠混用",
          pargs("/c 'a\\'b\\\\c'") == ["a'b\\c"], str(pargs("/c 'a\\'b\\\\c'")))
    check("只含转义单引号的参数", pargs("/c '\\''") == ["'"], str(pargs("/c '\\''")))
    check("单引号内 \\n 仍按框架约定转义",
          pargs("/c 'a\\nb'") == ["a\nb"], str(pargs("/c 'a\\nb'")))
    check("中文引号等价于 ASCII 引号",
          pargs("/c ‘a b’ “c d”") == ["a b", "c d"], str(pargs("/c ‘a b’ “c d”")))
    check("引号未配对时宽松回退（英文撇号不吞后半句）",
          pargs("/c We're BUPT") == ["We're", "BUPT"], str(pargs("/c We're BUPT")))
    _pkt_sub = _cp.parse("/buptnews get 'a b'")
    # 首尾空白（空格 / 制表符 / 换行 / 全角空格 / NBSP / CRLF）必须被忽略。
    # 用户实际发过 `"  /buptnews get 451\n"` 这种（首尾都有空白）形态。
    for raw in ("  /buptnews get 451\n",
                "\n/buptnews get 451\n",
                "/buptnews get 451   ",
                "\t/buptnews get 451\t",
                "\u3000/buptnews get 451",
                "\u00a0/buptnews get 451",
                "\r\n/buptnews get 451\r\n",
                "   /buptnews   get   451  "):
        pkt = _cp.parse(raw)
        ok = (pkt is not None and not pkt.illegal and pkt.command == "buptnews"
              and pkt.subcommand == "get" and pkt.args == ["451"])
        check(f"首尾空白被忽略 {raw!r}", ok,
              repr(pkt) if pkt else "None（未进命令系统）")

    from napcat.command import CommandManager as _CM
    from napcat.models import MessageEvent as _ME

    _cm = _CM(prefixes=["/"])
    _ws = _cp.parse("  [@3687155202] /buptnews get 451\n")
    check("首尾空白 + 前导 @机器人 都能处理",
          _ws is not None and _ws.command == "buptnews" and _ws.args == ["451"],
          repr(_ws) if _ws else "None")

    _only_ws = _cp.parse("/   \n\t ")
    # 归一化后命令名缺失 → 直接不进命令系统（返回 None；既不是命令、也不该被
    # 当成"未知命令"去回复用户）。早期实现会返回一个 illegal=True 的空命令包。
    check("命令名缺失（只有前缀+空白）→ 不进命令系统",
          _only_ws is None, repr(_only_ws))

    _cm = _CM(prefixes=["/"])

    @_cm.register("probe", description="探针")
    async def _probe(packet):
        # ⚠️ 无 schema 的命令由框架传**单个 packet**（不是 params, event）——
        # 签名写成 (params, event) 会抛 TypeError，被 handle 吞掉后返回 None，
        # 表现成"命令没反应"。这里踩过一次，留作记录。
        return "pong"

    _e2e = await _cm.handle(_ME(message_type="private", user_id=1,
                                message="  /probe\n"))
    check("端到端：首尾空白不影响命令分发", _e2e == "pong", repr(_e2e))

    _e2e_arr = await _cm.handle(_ME(message_type="private", user_id=1, message=[
        {"type": "text", "data": {"text": "\t/probe \n"}}]))
    check("端到端：消息段形态同样生效", _e2e_arr == "pong", repr(_e2e_arr))

    # 归一化入口是全框架单一来源
    from napcat.command import normalize_text as _norm
    check("normalize_text 去掉首尾空白与前导 @",
          _norm("  [@123] /buptnews get 451\n") == "/buptnews get 451",
          repr(_norm("  [@123] /buptnews get 451\n")))

    _blank_e2e = await _cm.handle(_ME(message_type="private", user_id=1,
                                      message="  /   \n\t "))
    check("端到端：只有前缀 + 空白不产生任何回复",
          _blank_e2e is None, repr(_blank_e2e))

    check("命令名/子命令解析不受转义影响",
          (_pkt_sub.command, _pkt_sub.subcommand, _pkt_sub.args) == ("buptnews", "get", ["a b"]),
          f"{_pkt_sub.command}/{_pkt_sub.subcommand}/{_pkt_sub.args}")

    print("\n[3] StateStore 持久化与去重")
    st = plugin._store
    check("初始未初始化", not st.is_initialized("1154"))
    st.mark_seen("_all", ["a:1", "a:2"])
    check("mark_seen 后可查询", st.is_seen("_all", "a:1")) 
    check("重复 mark_seen 不重复计数", st.mark_seen("_all", ["a:1"]) == 0)
    st.mark_initialized("1154")
    st.note_sync()
    st.note_push(3)
    st.save()
    st2 = StateStore(str(TMP / "data" / "state.json"))
    check("重载后 seen 保留", st2.is_seen("_all", "a:2"))
    check("重载后 initialized 保留", st2.is_initialized("1154"))
    check("重载后 pushedCount 保留", st2.pushed_count == 3, str(st2.pushed_count))
    st3 = StateStore(str(TMP / "data" / "cap.json"), max_seen=3)
    st3.mark_seen("_all", [f"k{i}" for i in range(5)])
    check("seen 上限裁剪生效", st3.seen_count("_all") == 3, str(st3.seen_count("_all")))

    # A10–A12：ID 分配、保留淘汰、跨重载递增
    nf = TMP / "data" / "ids.json"
    si = StateStore(str(nf), str(TMP / "data" / "ids-notices.jsonl"), retention=3)
    ids = [si.add_notice({"newsid": str(i), "treeid": "1", "title": f"t{i}"})
           for i in range(5)]
    check("A10 ID 从 1 起递增", ids[0] == 1 and ids == sorted(ids), str(ids))
    check("A12 超过 retention 后记录被淘汰", si.notice_count() <= 4,
          str(si.notice_count()))
    newest = ids[-1]
    si.update_notice(newest, summary="摘要X")
    check("摘要可回填", si.get_notice(newest)["summary"] == "摘要X")
    si.save()
    si2 = StateStore(str(nf), str(TMP / "data" / "ids-notices.jsonl"), retention=3)
    check("A11 重载后 ID 计数器不回退",
          si2.next_id() == ids[-1] + 1, f"{si2._data['idCounter']} vs {ids[-1]}")
    check("A12 淘汰后 get 返回 None", si2.get_notice(1) is None or si2.notice_count() < 5)

    # v1 -> v2 迁移：v1 的 pushed 是计数(int)，v2 的 pushed 是集合(list)
    v1f = TMP / "data" / "v1.json"
    v1f.write_text(json.dumps({"version": 1, "seen": {"_all": ["z:1"]},
                               "initialized": {"1154": True}, "lastSync": 1.0,
                               "pushed": 42}), encoding="utf-8")
    sv1 = StateStore(str(v1f))
    check("v1 seen 保留", sv1.is_seen("_all", "z:1"))
    check("v1 pushed 计数迁移到 pushedCount", sv1.pushed_count == 42,
          str(sv1.pushed_count))
    check("v1 pushed 集合被重置为空", sv1._data["pushed"] == [],
          str(sv1._data["pushed"]))

    # 自愈：notices.jsonl 已有记录，但 state.json 的 idCounter 落后
    # （真实场景：bot 运行期间从外部导入了历史通知，bot 又用旧内存状态覆盖了 state.json）
    hf = TMP / "data" / "heal.json"
    hf.write_text(json.dumps({"version": 2, "idCounter": 0, "seen": {},
                              "pushed": [], "initialized": {}}), encoding="utf-8")
    hn = TMP / "data" / "heal-notices.jsonl"
    with open(hn, "w", encoding="utf-8") as f:
        for i in (7, 8, 9):
            f.write(json.dumps({"id": i, "newsid": str(i), "treeid": "t",
                                "title": f"t{i}"}) + "\n")
    sh = StateStore(str(hf), str(hn))
    check("自愈：idCounter 被抬到既有记录的最大 ID",
          sh._data["idCounter"] == 9, str(sh._data["idCounter"]))
    check("自愈后新 ID 不与既有记录冲突", sh.next_id() == 10, str(sh.next_id()))
    check("自愈不会取更小的值（只增不减）",
          StateStore(str(hf), str(hn))._data["idCounter"] == 9)

    print("\n[4] 同步逻辑（桩门户 + FakeApi）")
    plugin.config.set("username", "u")
    plugin.config.set("password", "p")
    plugin.config.set("enabled", True)
    _push(plugin, groups=[GROUP_BOUND], users=[])
    plugin.config.set("first_run_backfill", 0)
    # 免打扰置成「同日同点」= 关闭：否则自检在北京时间 23:00–07:00 跑时推送会被抑制，
    # 出现「白天全绿、半夜全红」的时间敏感失败。需要免打扰的用例自己显式设窗口或用桩。
    plugin.config.set("quiet_hours", {"start": "00:00", "end": "00:00"})
    check("自检不受默认免打扰时段影响（任何时刻可跑）",
          not plugin._in_quiet_hours(),
          f"now={datetime.now():%H:%M}")

    # 离线确定性：天气与摘要都用桩件，绝不联网
    plugin._weather = FakeWeather()
    plugin._summarizer = FakeSummarizer()

    # 重置状态，模拟全新部署
    plugin._store = StateStore(str(TMP / "data" / "state2.json"),
                               str(TMP / "data" / "state2-notices.jsonl"))
    # 把「今天」标记为已早报，使本节推送是普通列表（早报路径另在 [8] 验证）
    plugin._store.note_digest(plugin._today())
    n_old = make_notice(Notice, "142431", "老通知一")
    n_mid = make_notice(Notice, "142432", "老通知二")
    plugin._portal = FakePortal([n_mid, n_old])

    r1 = await plugin._sync_once(reason="test")
    check("首次同步建立基线且不推送", r1 == (0, 0, 0), str(r1))
    check("首次同步后标记已初始化", plugin._store.is_initialized("1154"))
    check("首次同步未发送任何消息", len(api.sent) == 0, str(api.texts()))

    r2 = await plugin._sync_once(reason="test")
    check("无新通知时不推送", r2 == (0, 0, 0), str(r2))

    n_new = make_notice(Notice, "142499", "新通知：某某评比")
    plugin._portal = FakePortal([n_new, n_mid, n_old])
    r3 = await plugin._sync_once(reason="test")
    check("新增 1 条时推送 1 条", r3 == (1, 1, 0), str(r3))
    check("消息发到配置的群", api.sent and api.sent[-1][0] == "group" and api.sent[-1][1] == str(GROUP_BOUND))
    txt = api.texts()[-1][2]
    _body = [ln for ln in txt.splitlines() if ln.strip() and not ln.startswith("⚠")]
    check("A17 推送仅 `ID 摘要`（无 # 前缀）",
          _body and _body[0][0].isdigit() and "http" not in txt and "#" not in txt,
          repr(txt))
    check("A17 推送不含部门/日期/链接",
          "教务部" not in txt and "2026-09-23" not in txt, repr(txt))
    check("摘要取自 summarizer 而非标题", "摘要142499" in txt, repr(txt))
    check("A18 摘要不超 20 字",
          all(len(ln.split(" ", 1)[1]) <= 20
              for ln in txt.splitlines() if " " in ln), repr(txt))
    check("累计推送计数递增", plugin._store.pushed_count == 1,
          str(plugin._store.pushed_count))
    check("A3/A24 已推送键被标记", plugin._store.is_pushed(n_new.key))

    # 多目标：群 + 用户
    api.sent.clear()
    _push(plugin, users=[10000001])
    n_new2 = make_notice(Notice, "142500", "新通知：第二条")
    plugin._portal = FakePortal([n_new2, n_mid, n_old])
    r4 = await plugin._sync_once(reason="test")
    check("群+用户两个目标都发送", r4 == (1, 2, 0), str(r4))
    check("私聊目标收到消息", any(k == "private" and t == "10000001" for k, t, _ in api.sent))

    # 推送开关关闭 → 与免打扰同语义：落库但不推送，重新开启后补发
    api.sent.clear()
    plugin.config.set("enabled", False)
    n_new3 = make_notice(Notice, "142501", "新通知：第三条")
    plugin._portal = FakePortal([n_new3, n_mid, n_old])
    r5 = await plugin._sync_once(reason="test")
    check("关闭开关后不同步发送", r5 == (1, 0, 0), str(r5))
    check("关闭开关后确实没有发送", len(api.sent) == 0)
    check("关闭开关期间形成积压", len(plugin._backlog_records()) == 1,
          str(len(plugin._backlog_records())))
    plugin.config.set("enabled", True)

    # 多目标、多条通知 → 聚合成一条；积压与新通知合并推送
    api.sent.clear()
    _push(plugin, users=[])
    n_a = make_notice(Notice, "142601", "批量通知 A")
    n_b = make_notice(Notice, "142602", "批量通知 B")
    plugin._portal = FakePortal([n_b, n_a, n_new3, n_mid, n_old])
    r6 = await plugin._sync_once(reason="test")
    check("重新开启后补发积压共 3 条", r6 == (3, 1, 0), str(r6))
    check("积压已清空", len(plugin._backlog_records()) == 0,
          str(len(plugin._backlog_records())))
    agg = api.texts()[-1][2]
    _agg_body = [ln for ln in agg.splitlines()
                 if ln.strip() and not ln.startswith("⚠")]
    check("多条聚合成一条消息", len(_agg_body) == 3, repr(agg))
    check("聚合消息含两条新摘要",
          "摘要142601" in agg and "摘要142602" in agg, repr(agg))
    check("聚合消息也含积压那条", "摘要142501" in agg, repr(agg))

    # 预览模式：只私聊，不广播
    api.sent.clear()
    n_p = make_notice(Notice, "142700", "预览通知")
    plugin._portal = FakePortal([n_p, n_b, n_a, n_mid, n_old])
    r7 = await plugin._sync_once(reason="test", preview_user=SUPERUSER)
    check("preview 只发给调用者", len(api.sent) == 1 and api.sent[0][0] == "private"
          and api.sent[0][1] == str(SUPERUSER), str(api.sent))
    check("preview 不写入累计推送", plugin._store.pushed_count == 5,
          str(plugin._store.pushed_count))

    print("\n[5] 命令注册与分发")
    names = set(cmds.list_commands().keys())
    check("父命令 buptnews 已注册", "buptnews" in names)
    parent = cmds.list_commands().get("buptnews")
    subs = set(parent.subcommands.keys()) if parent else set()
    for expect in ("status", "list", "get", "targets", "add", "set", "del",
                   "dektoken", "on", "off", "interval", "sync", "images",
                   "search", "pin", "unpin"):
        check(f"子命令已注册 {expect}", expect in subs, str(sorted(subs)))
    check("子命令未占用顶层命名空间",
          not any(n.startswith("buptnews.") for n in names), str(sorted(names)))
    check("子命令挂在父命令下", parent is not None and len(parent.subcommands) == 16,
          str(sorted(parent.subcommands)) if parent else "no parent")

    check("定时任务已注册", "bupt_news.sync" in timer._metas, str(list(timer._metas)))

    # 命令分发：按插件**实际注册**的 scope/permission 断言（status 为
    # 「仅私聊 + superuser」，add/interval 等同级；list/sync 为「all + admin」）。
    async def dispatch(cmd, message_type="private", user_id=SUPERUSER, group_id=None):
        """返回 (原始结果, 文本) —— 元组即框架的隐藏命令哨兵"""
        ev = MessageEvent(
            message_type=message_type, user_id=user_id, group_id=group_id,
            message=[{"type": "text", "data": {"text": cmd}}],
            raw_message=cmd,
        )
        r = await cmds.handle(ev)
        if isinstance(r, tuple):
            return r, ""
        body = r.build() if hasattr(r, "build") else r
        text = ("".join(s.get("data", {}).get("text", "") for s in body)
                if isinstance(body, list) else str(r or ""))
        return r, text

    res, text = await dispatch("/buptnews status", "private", SUPERUSER)
    check("超级用户私聊可查 status", "推送状态" in text, text[:80])

    # scope 维度：status/enable 等子命令声明为仅私聊 → 群内执行（含超级用户）一律哨兵
    res, _ = await dispatch("/buptnews status", "group", SUPERUSER, GROUP_BOUND)
    check("status 仅私聊：群内执行返回隐藏命令哨兵",
          isinstance(res, tuple) and res[0] == "_not_found", repr(res))

    # 权限维度：普通成员私聊执行 add（需 superuser）应被拒绝
    res2, _ = await dispatch("/buptnews add user 12345678", "private", 10001)
    check("非超级用户无法 add 目标",
          12345678 not in plugin._targets()["users"],
          f"users={sorted(plugin._targets()['users'])}")
    # 框架以 ("_not_found", cmd) 隐藏命令存在性，客户端会回“未知命令”
    check("权限不足返回隐藏命令哨兵",
          isinstance(res2, tuple) and res2[0] == "_not_found", repr(res2))

    # 超级用户私聊可以 add / interval
    await dispatch("/buptnews add user 12345678", "private", SUPERUSER)
    check("超级用户可添加用户目标", 12345678 in plugin._targets()["users"],
          str(sorted(plugin._targets()["users"])))

    await dispatch("/buptnews interval 30m", "private", SUPERUSER)
    check("interval 支持 30m 人类可读格式", plugin.config.get("interval") == 1800,
          str(plugin.config.get("interval")))
    check("改动间隔后定时任务重建", "bupt_news.sync" in timer._metas)

    # 权限维度：群管理员（群内 admin，私聊按 member 计）不得执行 superuser 子命令。
    # 子命令自身的 permission 由框架在路由前校验（T1 修复）。
    perm = mgr.perm_manager
    perm.set_group_role(GROUP_BOUND, USER_ALLOWED, "admin")
    before_interval = plugin.config.get("interval")
    res5, _ = await dispatch("/buptnews interval 5m", "private", USER_ALLOWED)
    check("组管理员私聊不能改 interval（需 superuser）",
          plugin.config.get("interval") == before_interval,
          f"interval 被改成了 {plugin.config.get('interval')}")
    check("框架层拦截 superuser 子命令（隐藏命令哨兵）",
          isinstance(res5, tuple) and res5[0] == "_not_found", repr(res5))

    # scope 维度：同一子命令在群里同样拿不到（仅私聊）
    res5g, _ = await dispatch("/buptnews interval 5m", "group", USER_ALLOWED, GROUP_BOUND)
    check("群内执行仅私聊子命令同样返回哨兵",
          isinstance(res5g, tuple) and res5g[0] == "_not_found", repr(res5g))

    # on/off 也不该被非超级用户触发
    enabled_before = plugin.config.get("enabled")
    await dispatch("/buptnews off", "private", USER_ALLOWED)
    check("组管理员私聊不能关闭推送（需 superuser）",
          plugin.config.get("enabled") == enabled_before,
          f"enabled={plugin.config.get('enabled')}")

    await dispatch("/buptnews add user 12345679", "private", USER_ALLOWED)
    check("组管理员私聊不能添加目标（需 superuser）",
          12345679 not in plugin._targets()["users"],
          str(sorted(plugin._targets()["users"])))

    # admin 级子命令：sync 为 scope=all + admin；list/get 显式声明无权限
    res_list, _ = await dispatch("/buptnews list", "group", 10001, GROUP_BOUND)
    check("普通成员群内可执行 list（无权限限制，scope=all）",
          not isinstance(res_list, tuple), repr(res_list))
    res_get, _ = await dispatch("/buptnews get 1", "private", USER_ALLOWED)
    check("普通用户私聊可执行 get（无权限限制 + 在白名单内）",
          not isinstance(res_get, tuple), repr(res_get))
    res_get_g, _ = await dispatch("/buptnews get 1", "group", 10001, GROUP_BOUND)
    check("get 仅私聊：群内执行返回哨兵",
          isinstance(res_get_g, tuple) and res_get_g[0] == "_not_found", repr(res_get_g))
    res_sync, _ = await dispatch("/buptnews sync", "private", USER_ALLOWED)
    check("普通成员私聊不能 sync（需 admin）",
          isinstance(res_sync, tuple) and res_sync[0] == "_not_found", repr(res_sync))

    # T2 回归：包目录插件的监控文件集合必须覆盖 __init__.py 与全部子模块
    # （此前 watch() 只找 plugins/<name>.py，包目录插件热重载完全失效）
    pfiles = mgr._plugin_files("bupt_news")
    pnames = {p.name for p in pfiles}
    check("包目录插件被识别为多文件", len(pfiles) >= 5, str(sorted(pnames)))
    check("监控文件含 __init__.py 与 main.py",
          "__init__.py" in pnames and "main.py" in pnames, str(sorted(pnames)))
    check("包目录插件 mtime 已被记录",
          mgr._file_mtimes.get("bupt_news", 0) > 0,
          str(mgr._file_mtimes.get("bupt_news")))
    check("单文件插件的文件集合回退正确（不存在时不报错）",
          mgr._plugin_files("__no_such_plugin__") == [])

    print("\n[5.5] /help 权限指示（精确到子命令）")

    async def help_output(cmd: str, message_type: str = "private",
                          user_id: int = SUPERUSER, group_id=None) -> str:
        return (await dispatch(cmd, message_type, user_id, group_id))[1]

    def section_of(text: str, needle: str) -> str:
        """返回 needle 所在行所属的 [权限分组] 标题"""
        cur = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                cur = stripped
            elif needle in line:
                return cur
        return ""

    def line_of(text: str, needle: str) -> str:
        return next((l.strip() for l in text.splitlines() if needle in l), "")

    help_private = await help_output("/help")
    # 下面几条断言拿 **essential** 的 `/group_config`、`/ban` 当夹具锚点
    # （一个命令组 + 一条 admin 命令）。独立插件仓库里只有本插件 → 这几条跳过，
    # 而不是失败：它们测的是框架的 /help 版式，不是 bupt_news 的行为。
    has_essential = (PLUGIN_ROOT_DIR / "essential" / "__init__.py").exists()
    if not has_essential:
        print("  ⏭️  跳过：需要 essential 作命令夹具（不在本插件仓库里）")

    def fixture_check(name, cond, detail=""):
        if has_essential:
            check(name, cond, detail)

    help_group = await help_output("/help", "group", USER_ALLOWED, GROUP_BOUND)

    # 命令组（有子命令的命令）：本体不设权限，单独成组，不进任何 [权限] 分组
    check("私聊 /help：命令组本体不进权限分组",
          section_of(help_private, "/buptnews —") == "[📦 命令组]",
          section_of(help_private, "/buptnews —"))
    fixture_check("群聊 /help：命令组列在 [命令组]（buptnews/group_config）",
          section_of(help_group, "/buptnews —") == "[📦 命令组]"
          and section_of(help_group, "/group_config —") == "[📦 命令组]",
          section_of(help_group, "/buptnews —"))

    # 子命令不进第一级：第一级只列「命令组」与「没有子命令的命令」的介绍
    check("私聊 /help：第一级不含任何子命令行",
          not any(f"/buptnews {s}" in help_private for s in parent.subcommands),
          "\n".join(l for l in help_private.splitlines() if "buptnews" in l))
    check("群聊 /help：第一级同样不展开子命令",
          not any(f"/buptnews {s}" in help_group for s in parent.subcommands),
          "\n".join(l for l in help_group.splitlines() if "buptnews" in l))

    pm = mgr.perm_manager

    async def allowed_for(h, user_id, group_id, is_group=True):
        return h.allows_context(is_group) and await h.check_permission(
            user_id, group_id, is_group=is_group)

    # 详情视图（第二级）：命令组本体写明「无需权限 + 作用域」，子命令逐条标注自身权限与作用域
    detail = await help_output("/help buptnews")          # 超级用户私聊：全部子命令可见
    check("命令组详情：本体写明无需权限与作用域",
          "本体无需权限" in detail and "作用域：" in detail,
          detail.replace("\n", " / ")[:120])
    status_line = line_of(detail, "/buptnews status")
    check("详情：子命令行标注自身权限 + 作用域",
          status_line.startswith("/buptnews status [权限：superuser · 作用域：仅私聊]"),
          status_line)
    check("详情：子命令行确实没有参数占位符",
          "<" not in status_line and ">" not in status_line, status_line)

    # 第二级才列子命令：超级用户私聊看到全部
    listed_private = {s for s in parent.subcommands if f"/buptnews {s}" in detail}
    check("私聊 /help buptnews：列出全部子命令",
          listed_private == set(parent.subcommands),
          f"listed={sorted(listed_private)}")

    # 第二级的子命令可见性 = 自身 scope ∩ 调用者权限（status/add/on/off/interval/
    # targets/del/get 均为仅私聊，故群里不列）
    d_group = await help_output("/help buptnews", "group", USER_ALLOWED, GROUP_BOUND)
    listed_group = {s for s in parent.subcommands if f"/buptnews {s}" in d_group}
    expected_group = {s for s, h in parent.subcommands.items()
                      if await allowed_for(h, USER_ALLOWED, GROUP_BOUND)}
    check("群聊 /help buptnews：子命令可见性 = 自身 scope ∩ 调用者权限",
          listed_group == expected_group,
          f"listed={sorted(listed_group)} expected={sorted(expected_group)}")
    check("群聊 /help buptnews：不显示仅私聊子命令 status/add",
          "/buptnews status" not in d_group and "/buptnews add" not in d_group)
    check("群聊 /help buptnews：显示群内可用的 list/sync",
          "/buptnews list" in d_group and "/buptnews sync" in d_group)

    # 子命令详情（第三级）：权限/范围只在这里呈现
    sub_detail = await help_output("/help buptnews.status")   # 仅私聊命令 → 在私聊里查
    check("子命令详情：显示自身 superuser 与仅私聊",
          "权限：superuser" in sub_detail and "仅私聊" in sub_detail,
          sub_detail.replace("\n", " / "))
    sub_list = await help_output("/help buptnews.list")
    check("子命令详情：list 权限公开（不继承父命令 admin）+ 作用域所有场景",
          "权限：公开" in sub_list and "作用域：所有场景" in sub_list
          and "admin" not in sub_list,
          sub_list.replace("\n", " / ")[:120])

    # ---- /help 只列「调用者有权访问」的命令；版式不按权限分类 ----
    help_member = await help_output("/help", "group", 30001, GROUP_BOUND)
    help_admin = await help_output("/help", "group", USER_ALLOWED, GROUP_BOUND)
    check("第一级不再按权限分类（没有权限分组标题）",
          "[🔓 所有人可用]" not in help_member and "[🛡️ admin]" not in help_member
          and "[👑 superuser]" not in help_member, help_member[:160])
    fixture_check("无子命令的命令与子命令同版式：`[📖 命令]` 段 + `  /名字 — 介绍`",
          section_of(help_admin, "/ban —") == "[📖 命令]"
          and line_of(help_admin, "/ban —") == "/ban — 禁言成员", help_admin[:400])
    fixture_check("普通成员仍能看到命令组分区（子命令在第二级过滤）",
          section_of(help_member, "/buptnews —") == "[📦 命令组]"
          and section_of(help_member, "/group_config —") == "[📦 命令组]",
          help_member[:160])
    fixture_check("群管理员能看到 admin 命令、看不到 superuser 命令",
          "/ban —" in help_admin and "/bind —" not in help_admin,
          help_admin[:200])
    check("超级用户能看到 superuser 专属命令（同在 [📖 命令] 段）",
          section_of(help_private, "/bind —") == "[📖 命令]", help_private[:200])
    check("普通成员看不到 superuser 专属命令",
          "/buptnews add" not in help_member and "/bind" not in help_member,
          help_member[:140])

    def count_in_header(text: str) -> int:
        return int(text.splitlines()[0].split("（")[1].split("）")[0])

    def count_in_body(text: str) -> int:
        return sum(1 for l in text.splitlines()
                   if l.startswith("  /") and " — " in l)

    check("计数 = 命令组数 + 无子命令命令数（不含子命令）",
          count_in_header(help_private) == count_in_body(help_private)
          and count_in_header(help_group) == count_in_body(help_group),
          f"private={count_in_header(help_private)}/{count_in_body(help_private)} "
          f"group={count_in_header(help_group)}/{count_in_body(help_group)}")

    # 无权限的详情按「未知命令」处理（与 handle() 的隐藏口径一致）
    d_noperm = await help_output("/help buptnews.add", "group", 30001, GROUP_BOUND)
    check("普通成员查无权限命令详情 → 未知命令",
          "未知命令" in d_noperm, d_noperm[:80])
    d_admin_super = await help_output("/help buptnews.status", "group", USER_ALLOWED, GROUP_BOUND)
    check("群管理员查 superuser 专属子命令详情 → 未知命令",
          "未知命令" in d_admin_super, d_admin_super[:80])

    # 命令组本体不设权限：普通成员能看，但只列出自己可用的子命令
    d_member_ns = await help_output("/help buptnews", "group", 30001, GROUP_BOUND)
    check("普通成员能查看命令组详情且只列可用子命令",
          d_member_ns.startswith("📖 /buptnews")
          and "/buptnews list" in d_member_ns
          and "/buptnews status" not in d_member_ns
          and "/buptnews add" not in d_member_ns,
          d_member_ns[:160])

    # 私聊里的非超级用户：只看得到公开命令
    help_plain_private = await help_output("/help", "private", 30001)
    check("私聊非超级用户看不到 superuser 专属命令（/bind）",
          "/bind —" not in help_plain_private
          and section_of(help_plain_private, "/ping —") == "[📖 命令]",
          help_plain_private[:160])

    # 群聊需要 @机器人：/help 要明确提示（避免用户以为「复制命令就能用」）
    check("群聊 /help 提示需先 @机器人",
          "@机器人" in help_group and "直接复制" in help_group, help_group[:90])
    check("私聊 /help 不出现 @提示", "@机器人" not in help_private, help_private[:90])
    d_group_cmd = await help_output("/help ban", "group", USER_ALLOWED, GROUP_BOUND)
    fixture_check("群聊 /help <群内可用命令> 也提示 @",
                  "@机器人" in d_group_cmd, d_group_cmd[-90:])
    d_private_only = await help_output("/help ping", "group", USER_ALLOWED, GROUP_BOUND)
    check("群聊里查仅私聊命令与执行口径一致（按未知命令处理）",
          "未知命令" in d_private_only and "@机器人" not in d_private_only,
          d_private_only[:90])
    _, bn_group = await dispatch("/buptnews", "group", 30001, GROUP_BOUND)
    _, bn_private = await dispatch("/buptnews", "private", SUPERUSER)
    check("插件自带帮助在群里同样提示 @、私聊不提示",
          "@机器人" in bn_group and "@机器人" not in bn_private,
          f"group={bn_group[-60:]!r} private_has={'@机器人' in bn_private}")

    # 执行口径：命令组本体无需权限（普通成员可直接执行），但仍受自身 scope 约束
    res_ns, text_ns = await dispatch("/buptnews", "group", 30001, GROUP_BOUND)
    check("命令组本体无需权限：普通成员可直接执行",
          not isinstance(res_ns, tuple) and "校内通知推送" in text_ns, repr(res_ns))
    res_gc, _ = await dispatch("/group_config", "group", 30001, GROUP_BOUND)
    fixture_check("命令组本体无需权限：group_config 普通成员可执行",
          not isinstance(res_gc, tuple), repr(res_gc))
    res_gcp, _ = await dispatch("/group_config", "private", SUPERUSER)
    fixture_check("命令组本体仍受 scope 约束（group_config 仅群聊）",
          isinstance(res_gcp, tuple) and res_gcp[0] == "_not_found", repr(res_gcp))

    # /commands 调试视图同一口径：命令组本体不报权限，子命令逐条列出
    cmds_out = await help_output("/commands")
    ns_line = line_of(cmds_out, "/buptnews · ")
    sub_line = line_of(cmds_out, "/buptnews status")
    check("/commands：命令组本体不报权限", "权限：" not in ns_line, ns_line)
    check("/commands：列出子命令自身范围与说明",
          "/buptnews status" in sub_line and "仅私聊" in sub_line, sub_line)

    # 自定义权限节点也应出现在 /help（此前只打印 5 个内置分组，节点命令被整条吞掉）
    @cmds.register("vipdemo", permission="vip", description="自定义节点命令")
    async def cmd_vipdemo(packet=None):
        return "vip"

    try:
        vip_help = await help_output("/help")
        check("自定义权限节点命令出现在 /help（归入 [📖 命令] 段）",
              section_of(vip_help, "/vipdemo") == "[📖 命令]",
              section_of(vip_help, "/vipdemo"))
    finally:
        cmds.unregister("vipdemo")

    print("\n[5.7] 私聊可用范围（绑定群小窗 + 指定用户）")

    # 白名单夹具：只放行 GROUP_BOUND 的小窗与 USER_ALLOWED 的直接私聊
    check("夹具的绑定/白名单已生效（不吃默认值）",
          GROUP_BOUND in plugin._bound_groups()
          and USER_ALLOWED in plugin._bound_users(),
          f"groups={sorted(plugin._bound_groups())} users={sorted(plugin._bound_users())}")

    def access(message_type="private", user_id=USER_ALLOWED, group_id=None):
        """只问判定，不进命令分发 —— 便于逐条断言准入矩阵。"""
        return plugin.check_access(MessageEvent(
            message_type=message_type, user_id=user_id, group_id=group_id,
            message=[{"type": "text", "data": {"text": "/buptnews list"}}],
            raw_message="/buptnews list"))

    check("超级用户私聊豁免（否则白名单配错就没人能改回来）",
          access("private", SUPERUSER))
    check("白名单用户可直接私聊", access("private", USER_ALLOWED))
    check("非白名单用户的直接私聊被拒", not access("private", 10001))
    check("绑定群的小窗放行（用户不必在白名单里）",
          access("private", 10001, GROUP_BOUND))
    check("非绑定群的小窗被拒（同一用户在绑定群里仍然可用）",
          not access("private", 10001, 100000002))
    check("群聊不受范围限制（准入由框架 bound_groups 负责）",
          access("group", 10001, 100000002))
    check("无上下文（直接渲染帮助）不拦", plugin.check_access(None))

    # 强制点：命令处理器外面的守卫
    res_priv, _ = await dispatch("/buptnews list", "private", 10001)
    check("非白名单用户私聊执行命令被范围挡下（不是权限哨兵）",
          res_priv is None, repr(res_priv))
    res_temp, _ = await dispatch("/buptnews list", "private", 10001, GROUP_BOUND)
    check("绑定群小窗里同一用户可以执行", not isinstance(res_temp, tuple), repr(res_temp))
    res_g, _ = await dispatch("/buptnews list", "group", 10001, GROUP_BOUND)
    check("群里同一用户照旧可用（范围只管私聊）", not isinstance(res_g, tuple), repr(res_g))
    res_root, _ = await dispatch("/buptnews", "private", 10001)
    check("命令组本体私聊同样受范围约束", res_root is None, repr(res_root))

    # /help 与服务范围同口径：用不了的地址连功能清单都看不到
    help_before = await help_output("/help", "private", SUPERUSER)
    check("（前提前置）白名单用户的帮助里能看到本命名空间",
          "/buptnews" in help_before, help_before[:80])
    help_denied = await help_output("/help", "private", 10001)
    check("/help：不在范围内的私聊看不到本命名空间",
          "/buptnews" not in help_denied, help_denied[:120])
    d_denied = await help_output("/help buptnews", "private", 10001)
    check("/help <命令>：不在范围内按未知命令处理",
          d_denied.startswith("未知命令") and "子命令" not in d_denied, d_denied[:120])
    d_temp = await help_output("/help buptnews", "private", 10001, GROUP_BOUND)
    check("/help <命令>：绑定群小窗里看得到详情",
          "/buptnews" in d_temp and "子命令" in d_temp, d_temp[:120])

    # ⚠️ 这一整块会把 `targets` 写进配置（add/set/del 就是写配置），
    # 而后续同步用例仍在写旧的 groups/users —— 不恢复的话旧键会被
    # 「targets 已存在」的判定忽略，推送目标就变成这里留下的那批。
    _targets_snapshot = plugin.config.get("targets")
    try:
        # 绑定管理：`add` 已合并原来的 `bind`（一项绑定 = 准入 + 推送开关）
        tg = await help_output("/buptnews targets", "private", SUPERUSER)
        check("targets：列出绑定",
              "绑定" in tg and str(GROUP_BOUND) in tg, tg[:200])

        await dispatch("/buptnews add user 10001", "private", SUPERUSER)
        spec = plugin._targets()["users"].get(10001) or {}
        check("add user 建立绑定（默认推通知、不推活动）",
              spec.get("message") is True and spec.get("event") is False, str(spec))
        res_new, _ = await dispatch("/buptnews list", "private", 10001)
        check("★ 绑定即准入：add 之后立刻能私聊用命令",
              not isinstance(res_new, tuple), repr(res_new))

        before = len(plugin._targets()["users"])
        await dispatch("/buptnews add user 10001", "private", SUPERUSER)
        check("重复 add 幂等（不重复建条目）",
              len(plugin._targets()["users"]) == before, str(before))

        await dispatch("/buptnews set user 10001 event=on", "private", SUPERUSER)
        check("set 打开活动推送",
              plugin._targets()["users"][10001]["event"] is True,
              str(plugin._targets()["users"][10001]))
        await dispatch("/buptnews set user 10001 message=off", "private", SUPERUSER)
        check("set 关掉通知推送",
              plugin._targets()["users"][10001]["message"] is False,
              str(plugin._targets()["users"][10001]))
        await dispatch("/buptnews set user 10001", "private", SUPERUSER)
        check("set 不给开关时什么都不改（幂等）",
              plugin._targets()["users"][10001] == {"message": False, "event": True,
                                                   "token_file": plugin._targets()["users"][10001].get("token_file")},
              str(plugin._targets()["users"][10001]))
        _, bad = await dispatch("/buptnews set user 10001 event=maybe", "private", SUPERUSER)
        check("set 非法开关值有明确提示", "认不出" in bad, bad[:80])

        await dispatch("/buptnews del user 10001", "private", SUPERUSER)
        check("del 移除绑定", 10001 not in plugin._targets()["users"],
              str(sorted(plugin._targets()["users"])))
        res_rev, _ = await dispatch("/buptnews list", "private", 10001)
        check("★ 移除后立刻失效（私聊）", res_rev is None, repr(res_rev))
        res_temp2, _ = await dispatch("/buptnews list", "private", 10001, GROUP_BOUND)
        check("移除用户绑定后小窗仍可用（准入有两条独立通道）",
              not isinstance(res_temp2, tuple), repr(res_temp2))

        await dispatch("/buptnews add group 100000002", "private", SUPERUSER)
        check("add group 建立群绑定", 100000002 in plugin._targets()["groups"],
              str(sorted(plugin._targets()["groups"])))
        check("新加群的小窗立即可用", access("private", 10001, 100000002))
        await dispatch("/buptnews del group 100000002", "private", SUPERUSER)
        check("del group 撤销群绑定",
              plugin._bound_groups() == {GROUP_BOUND},
              f"groups={sorted(plugin._bound_groups())}")

        # 管理命令本身也走范围闸门（超管豁免），非超管连看都看不到
        res_ba, _ = await dispatch("/buptnews add user 10001", "private", USER_ALLOWED)
        check("非超管不能改绑定",
              10001 not in plugin._targets()["users"] and isinstance(res_ba, tuple),
              f"users={sorted(plugin._targets()['users'])} res={res_ba!r}")

        # bind 已删除：只剩 add（未知子命令会落到父命令的帮助）
        check("★ bind 已删除（合并进 add）", "bind" not in parent.subcommands,
              str(sorted(parent.subcommands)))

        # 默认值：targets 为空 → 只服务超级用户（最小权限），群聊不受影响
        keep_targets = plugin.config.get("targets")
        plugin.config.set("targets", {"groups": {}, "users": {}})
        try:
            check("默认（没有任何绑定）：非超管私聊一律不服务",
                  not access("private", USER_ALLOWED))
            check("默认：绑定群的来源小窗也不再放行",
                  not access("private", 10001, GROUP_BOUND))
            check("默认：超级用户照旧豁免", access("private", SUPERUSER))
            check("默认：群聊照旧不受限", access("group", 10001, GROUP_BOUND))
        finally:
            plugin.config.set("targets", keep_targets)

        # 写坏的配置不该炸掉命令：非字典段 / 列表 / bool 笔误 / 脏键
        for broken in ("nope", [1, 2], 123, None, {"groups": "x", "users": {}}, 
                       {"users": {"abc": True, "10001": "yes", "": {}}}):
            plugin.config.set("targets", broken)
            try:
                bound = plugin._bound_users()
                check(f"targets={broken!r} 写坏时不抛异常",
                      isinstance(bound, set), repr(bound))
            finally:
                plugin.config.set("targets", keep_targets)
        plugin.config.set("targets", {"groups": {}, "users": {
            "abc": {"message": True}, "10001": {"message": True},
            "-5": {"message": True}, "": {"message": True}}})
        check("绑定表里的脏键被丢弃、合法键被收下",
              plugin._bound_users() == {10001}, f"users={sorted(plugin._bound_users())}")
        plugin.config.set("targets", keep_targets)
    finally:
        plugin.config.set("targets", _targets_snapshot)

    check("被拒的私聊只记日志、不回任何提示（不暴露命令存在）",
          plugin._scope_denied.get(10001, (0.0, 0))[0] > 0,
          str(plugin._scope_denied.get(10001)))

    # 守卫的签名陷阱（回归）：包装器**必须**让框架看到 packet（守卫唯一的事件来源）
    # 与 params（框架只按参数名注入，`**kwargs` 不算）—— 少了任何一个，守卫要么
    # 拿不到事件而静默放行，要么原函数收不到参数而报 missing argument。
    for sub in sorted(parent.subcommands):
        h = parent.subcommands[sub]
        params_seen = tuple(inspect.signature(h.func).parameters)
        check(f"守卫签名暴露 packet+params（{sub}）",
              "packet" in params_seen and "params" in params_seen, str(params_seen))
    print("\n[6] 免打扰窗口与早报")
    plugin.config.set("quiet_hours", {"start": "23:00", "end": "07:00"})
    q_start, q_end = plugin._quiet_window()
    check("窗口解析为 23:00-07:00", (q_start.hour, q_end.hour) == (23, 7),
          f"{q_start}-{q_end}")

    def inq(h, m=0):
        return plugin._in_quiet_hours(datetime(2026, 9, 24, h, m))

    check("A1 22:59 非静默", not inq(22, 59))
    check("A1 23:00 静默", inq(23, 0))
    check("A1 23:30 静默", inq(23, 30))
    check("A1 00:30 静默（跨午夜）", inq(0, 30))
    check("A1 06:59 静默", inq(6, 59))
    check("A1 07:00 非静默", not inq(7, 0))
    check("A1 12:00 非静默", not inq(12, 0))

    # 同日窗口退化
    plugin.config.set("quiet_hours", {"start": "01:00", "end": "06:00"})
    check("同日窗口 02:00 静默", plugin._in_quiet_hours(datetime(2026, 9, 24, 2, 0)))
    check("同日窗口 07:00 非静默", not plugin._in_quiet_hours(datetime(2026, 9, 24, 7, 0)))
    plugin.config.set("quiet_hours", {"start": "00:00", "end": "00:00"})   # 恢复为关闭

    # A2/A3：静默期只落库，解除后补发
    api.sent.clear()
    plugin._store = StateStore(str(TMP / "data" / "q.json"),
                               str(TMP / "data" / "q-notices.jsonl"), retention=50)
    plugin._store.note_digest(plugin._today())      # 本段先测普通列表路径
    _push(plugin, groups=[GROUP_BOUND], users=[])
    plugin._summarizer = FakeSummarizer()
    n_base = make_notice(Notice, "150000", "基线通知")
    plugin._portal = FakePortal([n_base])
    await plugin._sync_once(reason="test")          # 先建立基线（首轮不推送）
    check("基线轮不发送", len(api.sent) == 0, str(api.texts()))

    n_q = make_notice(Notice, "150001", "静默期通知")
    plugin._portal = FakePortal([n_q, n_base])

    real_inq = plugin._in_quiet_hours
    plugin._in_quiet_hours = lambda now=None: True        # 强制静默
    r = await plugin._sync_once(reason="test")
    check("A2 静默期内不发送", len(api.sent) == 0 and r == (1, 0, 0), str(r))
    check("A2 静默期内仍已落库",
          plugin._store.get_notice(1) is not None
          and plugin._store.get_notice(1)["newsid"] == "150001",
          str(plugin._store.get_notice(1)))
    check("A2 静默期内已标记 seen", plugin._store.is_seen("_all", n_q.key))
    check("A2 静默期内未标记 pushed", not plugin._store.is_pushed(n_q.key))
    check("A2 积压可被识别", len(plugin._backlog_records()) == 1,
          str(len(plugin._backlog_records())))

    # ← 本次最重要的回归：静默期积压必须被补发，而不是永久丢失
    plugin._in_quiet_hours = lambda now=None: False       # 解除静默
    api.sent.clear()
    r = await plugin._sync_once(reason="test")
    check("A3 解除静默后补发积压", len(api.sent) == 1 and r == (1, 1, 0), str(r))
    check("A3 补发的正是静默期那条",
          "\n1 " in "\n" + api.texts()[-1][2], repr(api.texts()[-1][2]))
    check("A3 补发后标记 pushed", plugin._store.is_pushed(n_q.key))
    check("A3 补发后积压清空", len(plugin._backlog_records()) == 0,
          str(len(plugin._backlog_records())))

    n_q2 = make_notice(Notice, "150002", "静默后新通知")
    plugin._portal = FakePortal([n_q2, n_q, n_base])
    api.sent.clear()
    r = await plugin._sync_once(reason="test")
    check("A3 之后正常增量推送", len(api.sent) == 1 and r == (1, 1, 0), str(r))
    check("A3 新通知也标记 pushed", plugin._store.is_pushed(n_q2.key))
    check("A3 旧的不再重复推送",
          plugin._store.is_pushed(n_q.key) and len(plugin._backlog_records()) == 0)
    plugin._in_quiet_hours = real_inq

    # A4–A9：早报（天气行 + 列表）
    api.sent.clear()
    plugin._store = StateStore(str(TMP / "data" / "dg.json"),
                               str(TMP / "data" / "dg-notices.jsonl"), retention=50)
    plugin._store.note_digest("1999-01-01")     # 使本次可判为早报
    plugin._weather = FakeWeather("雷阵雨 18~27℃ 降水100%")
    n_d = make_notice(Notice, "150101", "早报通知")
    plugin._portal = FakePortal([n_d])
    r = await plugin._sync_once(reason="test")
    check("A3/A5 早报发出", len(api.sent) == 1, str(api.texts()))
    dg = api.texts()[-1][2]
    check("A6 首行为天气行", dg.splitlines()[0].startswith("☀️"), repr(dg))
    check("A6 天气行含地点与天气",
          "北京" in dg.splitlines()[0] and "雷阵雨" in dg.splitlines()[0], repr(dg))
    check("A5 早报后写入 lastDigestDate",
          plugin._store.last_digest_date == plugin._today(),
          str(plugin._store.last_digest_date))
    check("A4 同一天第二次不含天气行",
          not plugin._is_digest_due(), str(plugin._store.last_digest_date))
    check("降雨时天气行带带伞提醒", "☂️" in dg.splitlines()[0] and "记得带伞" in dg.splitlines()[0],
          repr(dg.splitlines()[0]))

    # A8：天气失败降级
    api.sent.clear()
    plugin._store.note_digest("1999-01-01")
    plugin._weather = FakeWeather(None)
    n_d2 = make_notice(Notice, "150102", "降级通知")
    plugin._portal = FakePortal([n_d2, n_d])
    await plugin._sync_once(reason="test")
    dg2 = api.texts()[-1][2]
    check("A8 天气失败仍发早报且降级",
          "天气暂不可用" in dg2 and dg2.splitlines()[0].startswith("☀️")
          and len(dg2.splitlines()) >= 2, repr(dg2)[:120])

    # A9：当天无新通知 → 只发天气行
    api.sent.clear()
    plugin._store.note_digest("1999-01-01")
    plugin._weather = FakeWeather("晴 18~27℃ 降水0%", umbrella=False,
                                  code=0, pop=0)
    plugin._portal = FakePortal([n_d2, n_d])       # 全部已见
    r = await plugin._sync_once(reason="test")
    check("A9 无新通知仍发早报", len(api.sent) == 1 and r == (0, 1, 0), str(r))
    only = api.texts()[-1][2]
    check("A9 只含天气行", len(only.splitlines()) == 1 and only.startswith("☀️"),
          repr(only))
    check("晴天不带带伞提醒", "☂️" not in only, repr(only))

    print("\n[7] 天气与摘要")
    wmod = sys.modules["napcat_plugin_bupt_news.weather"]
    smod = sys.modules["napcat_plugin_bupt_news.summarize"]

    check("A7 WMO 95 -> 雷阵雨", wmod.describe(95) == "雷阵雨")
    check("A7 WMO 0 -> 晴", wmod.describe(0) == "晴")
    check("A7 未知码回退", wmod.describe(1234) == "未知天气")
    check("A7 None 回退", wmod.describe(None) == "未知天气")
    check("A7 映射表覆盖 0-99 已知码无空洞",
          all(c in wmod.WMO_ZH for c in
              (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
               71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99)),
          str(sorted(wmod.WMO_ZH)))

    # ---- 带伞提醒规则 ----
    nu = wmod.needs_umbrella
    check("带伞：小雨 61", nu(61, 0) is True)
    check("带伞：阵雨 80", nu(80, 0) is True)
    check("带伞：雷阵雨 95", nu(95, 0) is True)
    check("带伞：冻雨 66", nu(66, 0) is True)
    check("带伞：雪 71", nu(71, 0) is True)
    check("带伞：阴天但降水概率 80%", nu(3, 80) is True)
    check("不带伞：晴 0", nu(0, 0) is False)
    check("不带伞：多云 2 且降水 10%", nu(2, 10) is False)
    check("不带伞：雾 45", nu(45, 5) is False)
    check("不带伞：码与概率都缺", nu(None, None) is False)
    check("带伞：阈值可调（降水 40% 在阈值 30 下提醒）",
          nu(3, 40, 30) is True and nu(3, 40, 50) is False)

    def wl(desc, umbrella, code=95, pop=100):
        return wmod.weather_line(
            wmod.Weather(code=code, pop=pop, text=desc, umbrella=umbrella), "北京")

    check("天气行：需要带伞时追加 ☂️",
          wl("小雨 10~15℃ 降水80%", True).endswith("☂️ 记得带伞"),
          repr(wl("小雨 10~15℃ 降水80%", True)))
    check("天气行：不需要带伞时不追加",
          "☂️" not in wl("晴 18~27℃ 降水0%", False))
    check("天气行：取不到天气时既降级也不提醒",
          "天气暂不可用" in wmod.weather_line(None, "北京")
          and "☂️" not in wmod.weather_line(None, "北京"))
    check("天气行：仍含日期与地点",
          all(x in wl("小雨 10~15℃ 降水80%", True) for x in ("北京", "小雨")))

    check("A20 解析裸 JSON",
          smod.parse_summaries('[{"id":"1","s":"摘要"}]') == {"1": "摘要"})
    check("A20 解析 ```json 围栏",
          smod.parse_summaries('```json\n[{"id":"2","s":"摘要二"}]\n```') == {"2": "摘要二"})
    check("A20 解析前后杂字",
          smod.parse_summaries('好的：[{"id":"3","s":"摘要三"}] 完毕') == {"3": "摘要三"})
    check("A20 坏 JSON 返回空", smod.parse_summaries("not json") == {})
    check("A18 clamp 截断到 20 字", len(smod.clamp("一" * 30)) == 20)
    check("A21 空 content 触发降级（解析为空）", smod.parse_summaries("") == {})
    check("A21 降级非空且不超 21 字",
          bool(smod.fallback_summary("某通知标题"))
          and len(smod.fallback_summary("标" * 40)) <= 21)
    check("A21 空标题降级不产生空串", smod.fallback_summary("") == "（无标题）")

    # A22：摘要失败 -> 全部降级为标题截断，仍按批次数调用
    api.sent.clear()
    plugin._store = StateStore(str(TMP / "data" / "sm.json"),
                               str(TMP / "data" / "sm-notices.jsonl"), retention=50)
    plugin._store.note_digest(plugin._today())
    bad = FakeSummarizer(fail=True)
    plugin._summarizer = bad
    n_b0 = make_notice(Notice, "150200", "基线通知B")
    plugin._portal = FakePortal([n_b0])
    await plugin._sync_once(reason="test")          # 先建立基线
    n_s = make_notice(Notice, "150201", "关于某某工作的正式通知文件")
    plugin._portal = FakePortal([n_s, n_b0])
    api.sent.clear()
    r = await plugin._sync_once(reason="test")
    check("A22 摘要失败仍推送", len(api.sent) == 1 and r == (1, 1, 0), str(r))
    sm = api.texts()[-1][2]
    _sm_body = [ln for ln in sm.splitlines() if ln.strip() and not ln.startswith("⚠")]
    check("A22 降级为标题截断",
          _sm_body and _sm_body[0][0].isdigit() and "关于某某工作" in sm
          and "#" not in sm, repr(sm))
    check("A22 未产生空摘要", all(ln.split(" ", 1)[1].strip()
                                  for ln in sm.splitlines() if " " in ln), repr(sm))

    # A19：N 条 -> ceil(N/batch_size) 次调用（用真 Summarizer 的批量逻辑，桩掉 _one_batch）
    from napcat_plugin_bupt_news.summarize import Summarizer as _Sum
    real_sum = _Sum(base_url="http://x", model="m", api_key="k", batch_size=3)
    calls = []

    async def fake_batch(items):
        calls.append(len(items))
        from napcat_plugin_bupt_news.summarize import NoticeDigest
        return {nid: NoticeDigest(f"批量摘要{nid}", True) for nid, _ in items}
    real_sum._one_batch = fake_batch
    many = [make_notice(Notice, str(160000 + i), f"通知{i}") for i in range(7)]
    got = await real_sum.summarize(many)
    check("A19 7 条按 batch_size=3 分 3 次调用", calls == [3, 3, 1], str(calls))
    check("A19 全部返回摘要", len(got) == 7 and "160000" in got, str(sorted(got)))
    await real_sum.close()

    # reasoning_effort 映射：none 进 payload；default/off 则完全不带该字段
    # （换到不认这个参数的网关时，把它设成 default 即可）
    class _Resp:
        status = 200
        async def json(self):
            return {"choices": [{"message": {"content": '[{"id":"1","s":"摘要"}]'},
                                 "finish_reason": "stop"}]}
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class _Sess:
        closed = False
        def __init__(self):
            self.payloads = []
        def post(self, url, json=None, headers=None):
            self.payloads.append(json)
            return _Resp()

    sess = _Sess()
    s_none = _Sum(base_url="http://x", model="m", api_key="k",
                  reasoning_effort="none")
    s_none._session = sess
    await s_none._one_batch([("1", "标题")])
    check("reasoning_effort=none 进入请求体",
          sess.payloads[-1].get("reasoning_effort") == "none", str(sess.payloads[-1]))
    s_def = _Sum(base_url="http://x", model="m", api_key="k",
                 reasoning_effort="default")
    s_def._session = sess
    await s_def._one_batch([("1", "标题")])
    check("reasoning_effort=default 不带该字段",
          "reasoning_effort" not in sess.payloads[-1], str(sess.payloads[-1]))
    check("默认构造即为 none（关思考链）",
          _Sum(base_url="http://x", model="m", api_key="k").reasoning_effort == "none")

    # A23/A24：天气不可达不阻塞；pushed 均正确标记
    check("A23/A24 天气降级后 pushed 已标记",
          plugin._store.is_pushed(n_s.key))

    print("\n[8] /buptnews get 查询")
    plugin._store = StateStore(str(TMP / "data" / "g.json"),
                               str(TMP / "data" / "g-notices.jsonl"), retention=50)
    n_g0 = make_notice(Notice, "170000", "基线通知G")
    plugin._store.note_digest(plugin._today())
    plugin._summarizer = FakeSummarizer()
    plugin._portal = FakePortal([n_g0])
    await plugin._sync_once(reason="test")          # 先建立基线（首轮不落库）
    n_g = make_notice(Notice, "170001", "关于可查询通知的测试")
    plugin.config.set("body_max_chars", 800)      # 显式设截断，验证截断行为
    plugin._portal = FakePortal([n_g, n_g0], body="这是正文内容。" * 200)
    await plugin._sync_once(reason="test")
    rec = plugin._store.get_notice(1)
    check("A13 记录已落库", rec is not None and rec["newsid"] == "170001", str(rec))

    def flat(r):
        """把命令返回值（str / MessageChain / 消息段列表 / 多段消息）拍成纯文本。"""
        if isinstance(r, (list, tuple)):
            if r and all(hasattr(x, "build") for x in r):
                return "".join(flat(x) for x in r)      # list[MessageChain]：分段发送
            return "".join(s.get("data", {}).get("text", "")
                           for s in r if isinstance(s, dict))
        b = r.build() if hasattr(r, "build") else r
        return flat(b) if isinstance(b, list) else str(b or "")

    evg = MessageEvent(message_type="private", user_id=SUPERUSER,
                       message=[{"type": "text", "data": {"text": "/buptnews get 1"}}],
                       raw_message="/buptnews get 1")
    tg = flat(await cmds.handle(evg))
    check("A13 get 返回标题", "关于可查询通知的测试" in tg, tg[:90])
    check("A13 get 返回部门与日期", "教务部" in tg and "2026-09-23" in tg, tg[:120])
    check("A13 get 返回正文", "这是正文内容" in tg, tg[:150])
    check("A14 get 返回原文页链接而非裸附件",
          "xntz_content.jsp" in tg and "download.jsp" not in tg, tg[-160:])
    check("A15 超长正文被截断到 body_max_chars",
          "…" in tg and len(tg) < 1200, f"len={len(tg)}")
    plugin.config.set("body_max_chars", 0)        # 默认值：不截断（长文交给分段发送）
    tg_full = flat(await cmds.handle(evg))
    check("body_max_chars=0 时不截断正文",
          "…" not in tg_full and len(tg_full) > len(tg),
          f"len={len(tg_full)} vs {len(tg)}")

    evg2 = MessageEvent(message_type="private", user_id=SUPERUSER,
                        message=[{"type": "text", "data": {"text": "/buptnews get 999999"}}],
                        raw_message="/buptnews get 999999")
    tg2 = flat(await cmds.handle(evg2))
    check("A16 未知 ID 明确提示且不抛异常", "未找到" in tg2, tg2[:90])

    # A25：静默窗口内 get 仍可用
    plugin._in_quiet_hours = lambda now=None: True
    tg3 = flat(await cmds.handle(evg))
    check("A25 静默期内 get 仍可用", "xntz_content.jsp" in tg3, tg3[-120:])
    plugin._in_quiet_hours = real_inq

    # list 语义已改为「分页列出编号」，targets 承接原「推送目标」
    async def run_cmd(text):
        return flat(await cmds.handle(MessageEvent(
            message_type="private", user_id=SUPERUSER,
            message=[{"type": "text", "data": {"text": text}}],
            raw_message=text)))

    # 造 45 条记录以便翻页（page_size 默认 20 → 3 页）
    plugin._store = StateStore(str(TMP / "data" / "pg.json"),
                               str(TMP / "data" / "pg-notices.jsonl"), retention=500)
    for i in range(45):
        plugin._store.add_notice({"key": f"t:{i}", "newsid": str(180000 + i),
                                  "treeid": "t", "title": f"分页测试通知{i}",
                                  "dept": "测试部", "date": "2026-09-24",
                                  "url": "http://x/", "body": "正文", "summary": ""})
    plugin.config.set("list_page_size", 20)

    def n_lines(t):
        return len([ln for ln in t.splitlines()
                    if ln[:1].isdigit() and " " in ln])

    p1 = await run_cmd("/buptnews list")
    check("分页：第 1 页标注总页数与总数",
          "第 1/3 页" in p1 and "共 45 条" in p1, p1[:70])
    check("分页：第 1 页 20 行编号", n_lines(p1) == 20, str(n_lines(p1)))
    check("分页：最新在前（45 在第 1 页）",
          any(ln.startswith("45 ") for ln in p1.splitlines()), p1[:220])
    check("分页：提示下一页", "list 2" in p1, p1[-90:])

    p2 = await run_cmd("/buptnews list 2")
    check("分页：第 2 页标注正确", "第 2/3 页" in p2, p2[:70])
    first_item_2 = next((ln for ln in p2.splitlines() if ln[:1].isdigit()), "")
    check("分页：第 2 页从 25 开始", first_item_2.startswith("25 "), first_item_2[:40])
    check("分页：第 2 页不含 45",
          not any(ln.startswith("45 ") for ln in p2.splitlines()), p2[:120])

    p3 = await run_cmd("/buptnews list 3")
    check("分页：末页只有 5 条", n_lines(p3) == 5, str(n_lines(p3)))
    check("分页：末页提示已是最后一页", "最后一页" in p3, p3[-90:])

    p9 = await run_cmd("/buptnews list 999")
    check("分页：越界页码夹到末页", "第 3/3 页" in p9, p9[:70])
    p0 = await run_cmd("/buptnews list 0")
    check("分页：页码 0 被 Schema 拒绝或夹到首页",
          isinstance(p0, str), repr(p0)[:60])
    pbad = await run_cmd("/buptnews list abc")
    check("分页：非法页码不崩溃", isinstance(pbad, str), repr(pbad)[:60])
    tt = flat(await cmds.handle(MessageEvent(
        message_type="private", user_id=SUPERUSER,
        message=[{"type": "text", "data": {"text": "/buptnews targets"}}],
        raw_message="/buptnews targets")))
    check("targets 返回绑定表", "绑定" in tt and "群" in tt, tt[:80])

    # get 的 Schema 校验：非数字应被拒（不抛异常）
    tbad = flat(await cmds.handle(MessageEvent(
        message_type="private", user_id=SUPERUSER,
        message=[{"type": "text", "data": {"text": "/buptnews get abc"}}],
        raw_message="/buptnews get abc")))
    check("get 非数字参数不崩溃", isinstance(tbad, str), repr(tbad)[:60])

    print("\n[8.5] 通知图片抓取与排版插入")

    # ---- 解析层：图片按正文偏移抽出，URL 绝对化，正文与纯文本提取器一致 ----
    page_url = "http://my.bupt.edu.cn/xntz_content.jsp?wbtreeid=2149&wbnewsid=7"
    d_img = pmod.parse_detail(DETAIL_IMAGES_FIXTURE, page_url)
    imgs = d_img["images"]
    body_img = d_img["body"]
    check("详情解析出 2 张图片", len(imgs) == 2, str(imgs))
    check("图片 URL 已绝对化",
          all(i["url"].startswith("http://my.bupt.edu.cn/") for i in imgs), str(imgs))
    check("data-src 懒加载写法也能取到",
          any(i["url"].endswith("images/pic2.png") for i in imgs), str(imgs))
    check("偏移落在对应段落之间（图一在第一、二段之间）",
          body_img[:imgs[0]["pos"]].rstrip().endswith("第一段文字，介绍背景。")
          and body_img[imgs[0]["pos"]:].lstrip().startswith("第二段文字"),
          f"pos={imgs[0]['pos']} body={body_img!r}")
    check("偏移落在对应段落之间（图二在第二、三段之间）",
          body_img[:imgs[1]["pos"]].rstrip().endswith("第二段文字，说明流程。")
          and body_img[imgs[1]["pos"]:].lstrip().startswith("第三段收尾"),
          f"pos={imgs[1]['pos']} body={body_img!r}")
    check("正文与纯文本提取器完全一致（未因图片改动排版）",
          pmod.extract_body_text(DETAIL_IMAGES_FIXTURE) == body_img, repr(body_img))
    check("正文不含 img 标签残留", "<img" not in body_img, body_img[:80])

    # ---- 渲染层：get 返回「文字段 + 图片段」交替的消息 ----
    plugin._store = StateStore(str(TMP / "data" / "img.json"),
                               str(TMP / "data" / "img-notices.jsonl"), retention=50)
    plugin.config.set("body_max_chars", 800)
    plugin._images.directory = str(TMP / "data" / "images")
    plugin._images.enabled = True
    plugin._images.min_prune_age = 0
    url1, url2 = imgs[0]["url"], imgs[1]["url"]
    plugin._store.add_notice({
        "key": "k:img1", "newsid": "190001", "treeid": "k", "title": "图片排版通知",
        "dept": "教务部", "date": "2026-09-24", "url": page_url,
        "body": body_img, "summary": "", "images": [dict(i) for i in imgs],
    })
    plugin._portal = FakePortal([], images=imgs)
    fake = plugin._portal

    async def run_get(cmd="/buptnews get 1"):
        r = await cmds.handle(MessageEvent(
            message_type="private", user_id=SUPERUSER,
            message=[{"type": "text", "data": {"text": cmd}}], raw_message=cmd))
        segs = r.build() if hasattr(r, "build") else r
        return segs if isinstance(segs, list) else [
            {"type": "text", "data": {"text": str(segs or "")}}]

    def seg_text(segs):
        return "".join(s.get("data", {}).get("text", "")
                       for s in segs if s.get("type") == "text")

    segs = await run_get()
    kinds = [s.get("type") for s in segs]
    idx_img = [i for i, s in enumerate(segs) if s.get("type") == "image"]
    check("get 返回图文混排消息（2 张图）", len(idx_img) == 2, str(kinds))
    before = seg_text(segs[:idx_img[0]])
    between = seg_text(segs[idx_img[0] + 1:idx_img[1]])
    after = seg_text(segs[idx_img[1] + 1:])
    check("标题/部门在首段文字里",
          "图片排版通知" in before and "教务部" in before, before[:60])
    check("第一张图之前只有第一段",
          "第一段文字" in before and "第二段文字" not in before, before)
    check("两张图之间是第二段",
          "第二段文字" in between and "第三段" not in between, between)
    check("末图之后是第三段与原文链接",
          "第三段收尾" in after and "原文（含附件）" in after, after[-80:])
    raw_files = [s["data"]["file"] for s in segs if s.get("type") == "image"]
    files = [unquote(urlparse(f).path) if f.startswith("file://") else f
             for f in raw_files]
    check("图片段用 file:// 引用本地缓存",
          all(f.startswith("file://") for f in raw_files), str(raw_files))
    check("图片已下载到本地缓存", all(os.path.isfile(f) for f in files), str(files))
    check("两张图片缓存为不同文件", files[0] != files[1], str(files))
    check("首轮按需下载 2 张", fake.image_calls == [url1, url2], str(fake.image_calls))

    await run_get()
    check("二次 get 命中缓存、不再下载", fake.image_calls == [url1, url2],
          str(fake.image_calls))

    # ---- 缓存清理：只保留仍被记录引用的图片 ----
    removed = plugin._images.prune([url1])
    check("缓存清理删除不再引用的图片",
          removed == 1 and not os.path.exists(files[1]), f"removed={removed}")
    check("仍被引用的图片被保留", os.path.exists(files[0]), str(files))

    # ---- 下载失败：只丢图，不丢正文 ----
    fake.image_fail = {url2}
    segs_fail = await run_get()
    txt_fail = seg_text(segs_fail)
    check("单张下载失败时其余内容完整",
          [s.get("type") for s in segs_fail].count("image") == 1
          and "第一段文字" in txt_fail and "第三段收尾" in txt_fail,
          str([s.get("type") for s in segs_fail]))
    check("失败张数在消息里说明", "1 张图片未获取到" in txt_fail, txt_fail[-70:])
    fake.image_fail = set()

    # ---- 有图片时不截断正文（截断会连图片一起丢，见「正文截断丢图」修复记录）----
    plugin.config.set("body_max_chars", imgs[1]["pos"])     # 即便上限卡在第 2 张图之前
    segs_cut = await run_get()
    check("有图片时跳过正文截断（图片不丢）",
          [s.get("type") for s in segs_cut].count("image") == 2,
          f"cut={imgs[1]['pos']} kinds={[s.get('type') for s in segs_cut]}")
    check("有图片时不出现截断标记", "…" not in seg_text(segs_cut),
          seg_text(segs_cut)[-40:])

    # 纯文字记录仍按 body_max_chars 截断
    cut_only_id = plugin._store.add_notice({
        "key": "k:cut", "newsid": "190004", "treeid": "k", "title": "纯文字截断通知",
        "dept": "教务部", "date": "2026-09-24", "url": page_url,
        "body": body_img, "summary": "", "images": [],
    })
    segs_cut2 = await run_get(f"/buptnews get {cut_only_id}")
    check("纯文字记录仍按 body_max_chars 截断",
          "…" in seg_text(segs_cut2), seg_text(segs_cut2)[-40:])
    plugin.config.set("body_max_chars", 800)

    # ---- 未补图的历史记录：明确提示 + 给出补齐入口 ----
    old_id = plugin._store.add_notice({
        "newsid": "190005", "treeid": "k", "title": "未补图的历史记录",
        "dept": "教务部", "date": "2026-09-18", "url": page_url,
        "body": "旧正文", "summary": "",
    })                                          # 故意不带 images 字段
    txt_old = flat(await cmds.handle(MessageEvent(
        message_type="private", user_id=SUPERUSER,
        message=[{"type": "text", "data": {"text": f"/buptnews get {old_id}"}}],
        raw_message=f"/buptnews get {old_id}")))
    check("未补图的历史记录会提示可补齐",
          "尚未补图" in txt_old and "/buptnews images" in txt_old, txt_old[-90:])

    # ---- 关闭图片：退化为纯文本（与原行为一致）----
    plugin._images.enabled = False
    segs_off = await run_get()
    check("images.enabled=false 时只发文本",
          [s.get("type") for s in segs_off].count("image") == 0
          and "第一段文字" in seg_text(segs_off), str([s.get("type") for s in segs_off]))
    plugin._images.enabled = True

    # ---- 边界：纯图片通知（正文为空）与相邻多图 ----
    two_id = plugin._store.add_notice({
        "key": "k:img2", "newsid": "190002", "treeid": "k", "title": "纯图片通知",
        "dept": "教务部", "date": "2026-09-24", "url": page_url,
        "body": "", "summary": "",
        "images": [{"pos": 0, "url": url1}, {"pos": 0, "url": url2}],
    })
    segs_two = await run_get(f"/buptnews get {two_id}")
    kinds_two = [s.get("type") for s in segs_two]
    two_files = [s["data"]["file"] for s in segs_two if s.get("type") == "image"]
    check("正文为空时仍发送图片，且不出现空的 📄 段",
          kinds_two.count("image") == 2 and "📄" not in seg_text(segs_two),
          str(kinds_two) + " | " + seg_text(segs_two)[:40])
    check("相邻图片按原顺序排列", two_files == raw_files, str(two_files))

    # ---- 同步落库：图片偏移随记录持久化 + 同步时预下载图片 ----
    async def sync_new_notice(tag: str, newsid_base: str, newsid_new: str,
                              images, fail_urls=None):
        """建基线（首轮不落库）→ 第二轮带图同步；返回 (fake_portal, 新记录)。"""
        plugin._store = StateStore(str(TMP / "data" / f"{tag}.json"),
                                   str(TMP / "data" / f"{tag}-notices.jsonl"),
                                   retention=50)
        plugin._store.note_digest(plugin._today())
        base = make_notice(Notice, newsid_base, f"基线通知{tag}")
        plugin._portal = FakePortal([base])
        await plugin._sync_once(reason="test")
        new = make_notice(Notice, newsid_new, f"带图通知{tag}")
        fake = FakePortal([new, base], body=body_img, images=list(images))
        fake.image_fail = set(fail_urls or ())
        plugin._portal = fake
        plugin._images.directory = str(TMP / "data" / f"imgs-{tag}")
        await plugin._sync_once(reason="test")
        return fake, plugin._store.get_notice(1)

    img_defaults = {"enabled": True, "max_bytes": 10 * 1024 * 1024,
                    "prefetch": True, "prefetch_max": 30,
                    "retry": 0, "retry_delay": 0.0, "max_attempts": 5}
    plugin.config.set("images", dict(img_defaults))
    fake_pf, rec_img = await sync_new_notice("192", "192000", "192001", imgs)
    check("同步时把图片偏移写入记录",
          rec_img is not None and rec_img.get("images") == imgs,
          str(rec_img.get("images") if rec_img else None))
    check("同步时预下载图片（get 前缓存已就绪）",
          fake_pf.image_calls == [url1, url2]
          and all(plugin._images.find(u) for u in (url1, url2)),
          f"calls={fake_pf.image_calls}")

    plugin.config.set("images", dict(img_defaults, prefetch=False))
    fake_off, rec_off = await sync_new_notice("193", "193000", "193001", imgs)
    check("images.prefetch=false 时同步不下载",
          fake_off.image_calls == [] and plugin._images.find(url1) is None,
          str(fake_off.image_calls))
    check("关闭预下载后记录仍带图片（get 时懒加载）",
          rec_off is not None and rec_off.get("images") == imgs)

    plugin.config.set("images", dict(img_defaults, prefetch_max=1))
    fake_cap, _ = await sync_new_notice("194", "194000", "194001", imgs)
    check("images.prefetch_max 限制单轮张数",
          fake_cap.image_calls == [url1], str(fake_cap.image_calls))

    plugin.config.set("images", dict(img_defaults, prefetch_max=0))
    fake_bad, rec_bad = await sync_new_notice("195", "195000", "195001", imgs,
                                              fail_urls={url2})
    check("预下载失败不影响同步落库",
          rec_bad is not None and rec_bad.get("images") == imgs,
          str(rec_bad.get("images") if rec_bad else None))
    check("失败的图片没进缓存、成功的已缓存",
          bool(plugin._images.find(url1)) and not plugin._images.find(url2),
          str(fake_bad.image_calls))
    check("失败的那张也尝试过下载",
          fake_bad.image_calls == [url1, url2], str(fake_bad.image_calls))
    plugin.config.set("images", dict(img_defaults))

    # ---- 同步补齐：历史保留记录里缺失的图片（本轮没有新通知也要跑）----
    url_a = "http://my.bupt.edu.cn/img/a.jpg"
    url_b = "http://my.bupt.edu.cn/img/b.jpg"

    def add_img_record(newsid: str, url: str) -> None:
        plugin._store.add_notice({
            "newsid": newsid, "treeid": "k", "title": f"历史通知{newsid}",
            "dept": "教务部", "date": "2026-09-20", "url": page_url,
            "body": "历史正文", "summary": "", "images": [{"pos": 0, "url": url}],
            # `tables` 必须给：缺这个键会被判定为"表格功能之前入库"而重抓正文页，
            # 从而抢占本用例要测的图片预取预算（曾经因此让下面几条断言拿到空列表）
            "tables": [],
        })

    plugin._store = StateStore(str(TMP / "data" / "196.json"),
                               str(TMP / "data" / "196-notices.jsonl"), retention=50)
    plugin._store.note_digest(plugin._today())
    plugin._images.directory = str(TMP / "data" / "imgs-196")
    add_img_record("196001", url_a)
    add_img_record("196002", url_b)
    check("补齐前缓存里没有这些图",
          plugin._images.find(url_a) is None and plugin._images.find(url_b) is None)
    fake_bf = FakePortal([make_notice(Notice, "196000", "基线196")])
    plugin._portal = fake_bf
    await plugin._sync_once(reason="test")          # 本轮无新通知（首轮只建基线）
    check("无新通知时也补齐历史记录缺失的图片",
          sorted(fake_bf.image_calls) == sorted([url_a, url_b]),
          str(fake_bf.image_calls))
    check("补齐后历史图片已在缓存中",
          bool(plugin._images.find(url_a)) and bool(plugin._images.find(url_b)))
    fake_bf.image_calls.clear()
    await plugin._sync_once(reason="test")
    check("已缓存的图片不再重复下载", fake_bf.image_calls == [], str(fake_bf.image_calls))

    # ---- 单轮上限 + 新记录优先 ----
    plugin.config.set("images", dict(img_defaults, prefetch_max=1))
    plugin._store = StateStore(str(TMP / "data" / "197.json"),
                               str(TMP / "data" / "197-notices.jsonl"), retention=50)
    plugin._store.note_digest(plugin._today())
    plugin._images.directory = str(TMP / "data" / "imgs-197")
    add_img_record("197001", url_a)                 # id 1（较旧）
    add_img_record("197002", url_b)                 # id 2（较新）
    fake_cap2 = FakePortal([make_notice(Notice, "197000", "基线197")])
    plugin._portal = fake_cap2
    await plugin._sync_once(reason="test")
    check("单轮上限生效且优先补最新的记录",
          fake_cap2.image_calls == [url_b], str(fake_cap2.image_calls))
    await plugin._sync_once(reason="test")
    check("下一轮继续补齐剩余的图片",
          fake_cap2.image_calls == [url_b, url_a], str(fake_cap2.image_calls))
    plugin.config.set("images", dict(img_defaults))

    async def run_img_cmd(text):
        return flat(await cmds.handle(MessageEvent(
            message_type="private", user_id=SUPERUSER,
            message=[{"type": "text", "data": {"text": text}}],
            raw_message=text)))

    # ---- 下载失败重试：立即重试 / 累计上限 / 命令强制重试 ----
    class FlakyPortal(FakePortal):
        """前 fail_times 次抛错，之后成功（模拟临时故障）。"""

        def __init__(self, *a, fail_times=1, **kw):
            super().__init__(*a, **kw)
            self.fail_times = fail_times

        async def fetch_bytes(self, url):
            self.image_calls.append(url)
            if self.image_calls.count(url) <= self.fail_times:
                raise RuntimeError("模拟临时下载故障")
            return self.image_bytes, self.image_ctype

    url_flaky = "http://my.bupt.edu.cn/img/flaky.jpg"
    plugin._store = StateStore(str(TMP / "data" / "200.json"),
                               str(TMP / "data" / "200-notices.jsonl"), retention=50)
    plugin._img_failures.clear()
    plugin._images.directory = str(TMP / "data" / "imgs-200")
    plugin._store.add_notice({
        "newsid": "200001", "treeid": "k", "title": "临时失败的图",
        "dept": "教务部", "date": "2026-09-19", "url": page_url,
        "body": "正文", "summary": "", "images": [{"pos": 0, "url": url_flaky}],
    })
    plugin.config.set("images", dict(img_defaults, retry=2, retry_delay=0.0))
    stats_retry = await plugin._prefetch_images()
    check("失败后立即重试直到成功",
          stats_retry["ok"] == 1 and plugin._images.find(url_flaky) is not None
          and len([c for c in plugin._img_failures.urls()]) == 0,
          f"stats={stats_retry} calls={plugin._img_failures.count()}")
    check("重试成功后失败账本清空", plugin._img_failures.count() == 0)

    # 一直失败：累计到 max_attempts 后自动补齐不再空撞
    url_dead = "http://my.bupt.edu.cn/img/dead.jpg"
    plugin._store.add_notice({
        "newsid": "200002", "treeid": "k", "title": "永久失败的图",
        "dept": "教务部", "date": "2026-09-19", "url": page_url,
        "body": "正文", "summary": "", "images": [{"pos": 0, "url": url_dead}],
    })
    fake_dead = FakePortal([])
    fake_dead.image_fail = {url_dead}
    plugin._portal = fake_dead
    plugin.config.set("images", dict(img_defaults, retry=0, max_attempts=2))
    s1 = await plugin._prefetch_images()
    s2 = await plugin._prefetch_images()
    calls_after_two = fake_dead.image_calls.count(url_dead)
    s3 = await plugin._prefetch_images()          # 第 3 次：已达上限 → 跳过
    check("失败次数被记账", plugin._img_failures.attempts(url_dead) == 2,
          str(plugin._img_failures.attempts(url_dead)))
    check("达到 max_attempts 后自动补齐跳过该图",
          s3["skipped"] == 1 and s3["tried"] == 0
          and fake_dead.image_calls.count(url_dead) == calls_after_two,
          f"s3={s3} calls={fake_dead.image_calls.count(url_dead)}")

    from napcat_plugin_bupt_news.images import FailureLog  # type: ignore
    plugin._img_failures.note_failure("http://my.bupt.edu.cn/img/persist.jpg", "boom")
    plugin._img_failures.save()
    reloaded = FailureLog(str(plugin._img_failures.path))
    check("失败账本落盘并可重载",
          reloaded.attempts("http://my.bupt.edu.cn/img/persist.jpg") == 1
          and "boom" in reloaded.error_of("http://my.bupt.edu.cn/img/persist.jpg"),
          f"attempts={reloaded.attempts('http://my.bupt.edu.cn/img/persist.jpg')}")
    r_stats2 = await run_img_cmd("/buptnews images")
    check("stats 显示失败待重试数量",
          "失败待重试" in r_stats2, r_stats2[:120])

    r_retry = await run_img_cmd("/buptnews images retry 5")
    check("retry 命令忽略上限、强制重试失败的图",
          fake_dead.image_calls.count(url_dead) > calls_after_two, r_retry[:100])
    check("retry 命令返回重试统计",
          "失败重试" in r_retry or "没有需要重试" in r_retry, r_retry[:120])
    plugin._img_failures.clear()
    plugin._portal = FakePortal([], images=imgs)
    plugin.config.set("images", dict(img_defaults))

    # ---- 历史记录补图：重抓没有 images 字段的老记录 ----
    from napcat_plugin_bupt_news.backfill import records_missing_images  # type: ignore

    def seed_no_image_records(tag: str, count: int) -> None:
        for i in range(1, count + 1):
            plugin._store.add_notice({
                "newsid": f"{tag}{i:03d}", "treeid": "k", "title": f"历史无图{tag}-{i}",
                "dept": "教务部", "date": "2026-09-19", "url": page_url,
                "body": "旧正文（入库时还没有图片功能）", "summary": "",
            })                                  # 注意：故意不带 images 字段

    plugin.config.set("images", dict(img_defaults, recrawl=True, recrawl_max=1))
    plugin._store = StateStore(str(TMP / "data" / "198.json"),
                               str(TMP / "data" / "198-notices.jsonl"), retention=50)
    plugin._store.note_digest(plugin._today())
    plugin._images.directory = str(TMP / "data" / "imgs-198")
    seed_no_image_records("198", 3)
    check("存在没有 images 字段的历史记录",
          len(records_missing_images(plugin._store)) == 3,
          str(len(records_missing_images(plugin._store))))
    plugin._portal = FakePortal([make_notice(Notice, "198000", "基线198")],
                                body=body_img, images=imgs)
    await plugin._sync_once(reason="test")
    check("同步时重抓历史记录（受 recrawl_max 限制，每轮 1 条）",
          len(records_missing_images(plugin._store)) == 2,
          str(len(records_missing_images(plugin._store))))
    rec_rc = plugin._store.get_notice(1)
    check("重抓后补出图片偏移，并刷新了正文",
          rec_rc is not None and rec_rc.get("images") == imgs
          and "第一段文字" in (rec_rc.get("body") or ""),
          str(rec_rc.get("images") if rec_rc else None))
    check("重抓发现的图片同步下载进缓存",
          all(plugin._images.find(u) for u in (url1, url2)),
          str([plugin._images.find(u) for u in (url1, url2)]))
    await plugin._sync_once(reason="test")
    check("下一轮继续补下一条历史记录",
          len(records_missing_images(plugin._store)) == 1,
          str(len(records_missing_images(plugin._store))))

    # ---- /buptnews images 命令 ----
    r_stats = await run_img_cmd("/buptnews images")
    check("images 命令显示缓存统计",
          "待重抓历史记录" in r_stats and "待下载" in r_stats and "⚙️ 策略" in r_stats,
          r_stats[:120])
    r_bf = await run_img_cmd("/buptnews images backfill 5")
    check("images backfill 把剩余历史记录补齐",
          len(records_missing_images(plugin._store)) == 0,
          str(len(records_missing_images(plugin._store))))
    check("images backfill 返回统计",
          "图片补齐完成" in r_bf and "剩余待补记录" in r_bf, r_bf[:140])
    r_dl = await run_img_cmd("/buptnews images download")
    check("images download 返回下载统计", "图片下载" in r_dl, r_dl[:80])

    # 关闭历史补图开关后不再重抓
    plugin.config.set("images", dict(img_defaults, recrawl=False))
    plugin._store = StateStore(str(TMP / "data" / "199.json"),
                               str(TMP / "data" / "199-notices.jsonl"), retention=50)
    plugin._store.note_digest(plugin._today())
    plugin._images.directory = str(TMP / "data" / "imgs-199")
    seed_no_image_records("199", 2)
    plugin._portal = FakePortal([make_notice(Notice, "199000", "基线199")],
                                body=body_img, images=imgs)
    await plugin._sync_once(reason="test")
    check("images.recrawl=false 时不同步重抓",
          len(records_missing_images(plugin._store)) == 2,
          str(len(records_missing_images(plugin._store))))
    plugin.config.set("images", dict(img_defaults))

    print("\n[8.6] 长消息分段发送")

    # ---- 框架层：MessageChain.split ----
    long_text = "\n".join(f"第{i}段" + "字" * 40 for i in range(1, 11))
    parts = MessageChain().text(long_text).split(max_chars=120, max_images=0)
    check("纯文字按段落切成多块", len(parts) >= 4, str(len(parts)))
    check("切块后文字与原消息完全一致",
          "".join(p.build()[0]["data"]["text"] for p in parts) == long_text)
    check("每块不超过字符预算",
          all(len(p.build()[0]["data"]["text"]) <= 120 for p in parts),
          str([len(p.build()[0]["data"]["text"]) for p in parts]))

    chain_mix = MessageChain()
    for i in range(1, 8):
        chain_mix.text(f"\n段落{i}").image(file=f"file:///tmp/{i}.jpg")
    mixed = chain_mix.split(max_chars=1000, max_images=3)
    check("图片按配额分块（每块 ≤3 张）",
          all(len([s for s in p.build() if s["type"] == "image"]) <= 3 for p in mixed),
          str([len([s for s in p.build() if s["type"] == "image"]) for p in mixed]))
    check("分块后图文顺序不变",
          [s["data"]["file"].rsplit("/", 1)[-1]
           for p in mixed for s in p.build() if s["type"] == "image"]
          == [f"{i}.jpg" for i in range(1, 8)])
    check("max_chars=0 / max_images=0 表示不限制",
          len(chain_mix.split(max_chars=0, max_images=0)) == 1)
    check("空消息也返回一条（不会发出 0 条）", len(MessageChain().split()) == 1)
    check("超长无换行文字会硬切",
          [len(p.build()[0]["data"]["text"])
           for p in MessageChain().text("y" * 250).split(max_chars=100, max_images=0)]
          == [100, 100, 50])

    # ---- 插件层：/buptnews get 按配额分段 ----
    plugin.config.set("images", dict(img_defaults))
    plugin.config.set("body_max_chars", 0)
    plugin._images.directory = str(TMP / "data" / "images")   # 与 raw_files 同一缓存目录
    plugin._portal = FakePortal([], images=imgs)              # 干净的桩（无失败注入）
    # 独立 store：只放本节要用的记录，避免受前面用例影响
    plugin._store = StateStore(str(TMP / "data" / "chunk.json"),
                               str(TMP / "data" / "chunk-notices.jsonl"), retention=50)
    chunk_id = plugin._store.add_notice({
        "key": "k:chunk", "newsid": "198001", "treeid": "k", "title": "图片排版通知",
        "dept": "教务部", "date": "2026-09-24", "url": page_url,
        "body": body_img, "summary": "", "images": [dict(i) for i in imgs],
    })

    async def get_chain(notice_id):
        r = await cmds.handle(MessageEvent(
            message_type="private", user_id=SUPERUSER,
            message=[{"type": "text", "data": {"text": f"/buptnews get {notice_id}"}}],
            raw_message=f"/buptnews get {notice_id}"))
        return r

    plugin.config.set("chunk", {"max_chars": 0, "max_images": 0})
    r_whole = await get_chain(chunk_id)
    whole_chain = r_whole[0] if isinstance(r_whole, list) else r_whole
    whole_text = seg_text(whole_chain.build())

    plugin.config.set("chunk", {"max_chars": 500, "max_images": 1})
    r_long = await get_chain(chunk_id)
    check("get 图片超配额时返回多条消息",
          isinstance(r_long, list) and len(r_long) >= 2,
          f"{type(r_long).__name__} n={len(r_long) if isinstance(r_long, list) else 1}")
    chunks = r_long if isinstance(r_long, list) else [r_long]
    check("每段消息图片数不超过 max_images",
          all(len([s for s in c.build() if s["type"] == "image"]) <= 1 for c in chunks),
          str([len([s for s in c.build() if s["type"] == "image"]) for c in chunks]))
    check("分段后图片顺序不变",
          [s["data"]["file"] for c in chunks for s in c.build() if s["type"] == "image"]
          == raw_files, str([s["data"]["file"] for c in chunks
                             for s in c.build() if s["type"] == "image"]))
    joined = "".join(seg_text(c.build()) for c in chunks)
    check("分段后文字与未分段完全一致", joined == whole_text,
          f"len(joined)={len(joined)} len(whole)={len(whole_text)}")

    # 纯文字长正文：按字符预算分段
    long_body = "\n".join(f"第{i}段正文内容" for i in range(1, 60))
    long_id = plugin._store.add_notice({
        "key": "k:long", "newsid": "190003", "treeid": "k", "title": "超长正文通知",
        "dept": "教务部", "date": "2026-09-24", "url": page_url,
        "body": long_body, "summary": "", "images": [],
    })
    plugin.config.set("chunk", {"max_chars": 120, "max_images": 0})
    r_txt = await cmds.handle(MessageEvent(
        message_type="private", user_id=SUPERUSER,
        message=[{"type": "text", "data": {"text": f"/buptnews get {long_id}"}}],
        raw_message=f"/buptnews get {long_id}"))
    check("纯文字长正文查询命中记录", not isinstance(r_txt, str), repr(r_txt)[:60])
    txt_chunks = r_txt if isinstance(r_txt, list) else [r_txt]
    check("纯文字长正文也会分段",
          len(txt_chunks) >= 5, f"chunks={len(txt_chunks)}")
    check("每段不超过 max_chars",
          all(len(seg_text(c.build())) <= 120 for c in txt_chunks),
          str([len(seg_text(c.build())) for c in txt_chunks]))
    check("长正文分段后内容无丢失",
          all(f"第{i}段正文内容" in "".join(seg_text(c.build()) for c in txt_chunks)
              for i in (1, 20, 40, 59)))

    # ---- 推送/早报过长同样分段 ----
    api.sent.clear()
    plugin.config.set("chunk", {"max_chars": 120, "max_images": 0})
    _push(plugin, groups=[GROUP_BOUND], users=[])
    plugin.config.set("enabled", True)
    digest_recs = [{"id": i, "newsid": str(i), "title": f"通知标题{i}",
                    "summary": ""} for i in range(1, 9)]
    digest_summaries = {str(i): "摘要内容" * 6 for i in range(1, 9)}
    await plugin._push_chain(plugin._build_list(digest_recs, digest_summaries))
    check("推送过长时同一目标收到多条消息", len(api.sent) >= 2, str(len(api.sent)))
    check("分段消息都发到配置的群",
          all(s[0] == "group" and s[1] == str(GROUP_BOUND) for s in api.sent),
          str(api.sent)[:100])

    # 复原配置，避免影响后续用例
    plugin.config.set("chunk", {"max_chars": 1200, "max_images": 4})
    plugin.config.set("body_max_chars", 800)
    _push(plugin, groups=[])

    # ---- 框架层：命令返回 list 时，客户端按顺序逐条发送 ----
    from napcat.client import NapCatClient

    class _ReplyStub(NapCatClient):
        """只借用 _reply_command 的发送逻辑，不启动真实客户端。"""

        def __init__(self, api):
            self.api = api
            self.logger = logging.getLogger("test.reply")

    stub = _ReplyStub(FakeApi())
    ev_reply = MessageEvent(message_type="private", user_id=SUPERUSER,
                            message=[{"type": "text", "data": {"text": "x"}}],
                            raw_message="x")
    await stub._reply_command(ev_reply, [MessageChain().text("A" * 30),
                                         MessageChain().text("B" * 30)])
    sent_txt = [t[2] for t in stub.api.texts()]
    check("命令返回 list 时逐条发送",
          len(sent_txt) == 2 and sent_txt[0] == "A" * 30 and sent_txt[1] == "B" * 30,
          str(sent_txt))
    await stub._reply_command(ev_reply, "普通回复")
    check("返回字符串仍只发一条", len(stub.api.sent) == 3, str(len(stub.api.sent)))

    class _FailApi(FakeApi):
        async def send_private_msg(self, user_id, message, auto_escape=False, group_id=None):
            self.sent.append(("private", str(user_id), message))
            raise ConnectionError("断线了")

    stub_fail = _ReplyStub(_FailApi())
    await stub_fail._reply_command(ev_reply, [MessageChain().text("A"),
                                              MessageChain().text("B"),
                                              MessageChain().text("C")])
    check("断线后剩余分段不再尝试（只发 1 次）",
          len(stub_fail.api.sent) == 1, str(len(stub_fail.api.sent)))

    print("\n[8.7] 消息排版（分区/缩进/导航/提示行）")

    plugin._store = StateStore(str(TMP / "data" / "fmt.json"),
                               str(TMP / "data" / "fmt-notices.jsonl"), retention=200)
    for i in range(1, 26):
        plugin._store.add_notice({
            "key": f"k:fmt{i}", "newsid": f"1990{i:02d}", "treeid": "k",
            "title": f"排版测试通知{i}", "dept": "保卫处", "date": "2026-09-24",
            "url": page_url, "body": "正文第一段。\n正文第二段。",
            "summary": f"第{i}条摘要", "images": [],
        })
    plugin.config.set("list_page_size", 20)
    _push(plugin, groups=[GROUP_BOUND], users=[10000001])

    st_txt = await run_img_cmd("/buptnews status")
    check("status：有分区标题（⚙️ 运行 / 📬 目标 / 📊 记录）",
          all(k in st_txt for k in ("⚙️ 运行", "📬 目标", "📊 记录")), st_txt[:120])
    indented = [ln for ln in st_txt.splitlines() if ln.startswith("  ") and "：" in ln]
    check("status：字段行统一两空格缩进（且分区标题顶格）",
          len(indented) >= 8
          and all(ln[:1] not in (" ", "\t") or ln.startswith("  ")
                  for ln in st_txt.splitlines()),
          f"indented={len(indented)}")
    check("status：状态用 ✅/⏸ 标记",
          ("✅ 开" in st_txt) or ("⏸ 关" in st_txt), st_txt[:80])

    img_txt = await run_img_cmd("/buptnews images")
    check("images：有 现状 / 策略 / 用法 三段",
          all(k in img_txt for k in ("📦 现状", "⚙️ 策略", "💡 用法")), img_txt[:160])
    check("images：用法段逐条列出命令",
          img_txt.count("/buptnews images ") >= 3, img_txt[-160:])

    lp1 = await run_img_cmd("/buptnews list")
    check("list：标题含页码/总数并用 · 分隔",
          "（第 1/2 页 · 共 25 条 · 最新在前）" in lp1, lp1[:80])
    check("list：条目与导航之间有空行分隔",
          "\n\n➡️ 下一页" in lp1, repr(lp1[-120:]))
    check("list：导航行含上一页/下一页/末页提示之一",
          any(k in lp1 for k in ("⬅️ 上一页", "➡️ 下一页", "✅ 已是最后一页")),
          lp1[-80:])
    check("list：结尾有 💡 用法提示",
          "查看全文" in lp1.rstrip().splitlines()[-1]
          and "search" in lp1.rstrip().splitlines()[-1],
          lp1[-80:])
    lp2 = await run_img_cmd("/buptnews list 2")
    check("list：末页同时给上一页与末页标记",
          "⬅️ 上一页" in lp2 and "✅ 已是最后一页" in lp2, lp2[-90:])

    get_txt = await run_img_cmd("/buptnews get 1")
    check("get：首行为 `📢 #编号 标题`", get_txt.splitlines()[0].startswith("📢 #1 "),
          get_txt.splitlines()[0])
    check("get：头部与正文之间有分隔线",
          any(set(ln.strip()) == {"─"} for ln in get_txt.splitlines()), get_txt[:120])
    check("get：结尾是 🔗 原文链接行",
          get_txt.rstrip().splitlines()[-1].startswith("🔗 原文（含附件）："), get_txt[-80:])

    tg_txt = await run_img_cmd("/buptnews targets")
    check("targets：分群/用户两段并带数量",
          "👥 群（" in tg_txt and "👥 用户（" in tg_txt, tg_txt[:160])
    check("targets：标出通知/活动开关",
          "通知" in tg_txt and "活动" in tg_txt, tg_txt[:200])
    check("targets：结尾给增删用法",
          "add group|user" in tg_txt and "/buptnews del" in tg_txt,
          tg_txt[-160:])

    help_txt = await run_img_cmd("/help")
    check("help：首行是 `📖 可用命令（N）`",
          help_txt.splitlines()[0].startswith("📖 可用命令（"), help_txt.splitlines()[0])
    check("help：只有 `[📦 命令组]` 与 `[📖 命令]` 两个分段标题",
          "[📦 命令组]" in help_txt and "[📖 命令]" in help_txt
          and "[🔓 所有人可用]" not in help_txt
          and "[👑 superuser]" not in help_txt, help_txt[:200])
    check("help：命令组段在前、命令段在后",
          help_txt.index("[📦 命令组]") < help_txt.index("[📖 命令]"),
          f"组={help_txt.index('[📦 命令组]')} 命令={help_txt.index('[📖 命令]')}")
    check("help：结尾是 💡 用法提示",
          help_txt.rstrip().endswith("查看子命令或用法"), help_txt[-60:])

    detail_txt = await run_img_cmd("/help buptnews")
    check("help 详情：首行 📖 + 命令名，含 `子命令` 块",
          detail_txt.startswith("📖 /buptnews") and "\n  子命令\n" in detail_txt,
          detail_txt[:80])
    check("help 详情：子命令行标注权限 + 作用域",
          "[权限：superuser · 作用域：仅私聊]" in detail_txt
          and "[权限：公开 · 作用域：" in detail_txt,
          detail_txt[:200])
    leaf_txt = await run_img_cmd("/help buptnews.get")
    check("help 叶子：`权限：公开 · 作用域：仅私聊` 与空默认值不再显示 `[默认: ]`",
          "权限：公开 · 作用域：仅私聊" in leaf_txt and "[默认: ]" not in leaf_txt, leaf_txt[:200])

    print("\n[8.8] 群聊临时会话（小窗）回复")

    from napcat.api import ApiClient
    from napcat.client import NapCatClient
    from napcat.models import ApiResponse

    temp_ev = MessageEvent(
        message_type="private", sub_type="group", user_id=SUPERUSER,
        group_id=GROUP_BOUND,
        message=[{"type": "text", "data": {"text": "/buptnews status"}}],
        raw_message="/buptnews status")
    plain_ev = MessageEvent(
        message_type="private", user_id=SUPERUSER,
        message=[{"type": "text", "data": {"text": "/buptnews status"}}],
        raw_message="/buptnews status")
    group_ev = MessageEvent(
        message_type="group", user_id=SUPERUSER, group_id=GROUP_BOUND,
        message=[{"type": "text", "data": {"text": "/buptnews status"}}],
        raw_message="/buptnews status")

    check("临时会话识别：private + 来源群",
          temp_ev.is_temp_session and not plain_ev.is_temp_session
          and not group_ev.is_temp_session,
          f"temp={temp_ev.is_temp_session} plain={plain_ev.is_temp_session} "
          f"group={group_ev.is_temp_session}")

    class _ReplyStub(NapCatClient):
        """只借用 _reply_command 的发送逻辑。"""

        def __init__(self, api):
            self.api = api
            self.logger = logging.getLogger("test.reply.temp")

    stub = _ReplyStub(FakeApi())
    await stub._reply_command(temp_ev, "小窗回复")
    check("小窗回复带上来源群号",
          stub.api.sent[-1][0] == "private"
          and stub.api.temp_sessions == [(str(SUPERUSER), str(GROUP_BOUND))],
          str(stub.api.temp_sessions))
    await stub._reply_command(plain_ev, "私聊回复")
    check("普通私聊回复不带 group_id",
          len(stub.api.temp_sessions) == 1 and stub.api.sent[-1][0] == "private",
          str(stub.api.temp_sessions))
    await stub._reply_command(group_ev, "群回复")
    check("群消息照旧走群发送",
          stub.api.sent[-1][0] == "group" and stub.api.sent[-1][1] == str(GROUP_BOUND),
          str(stub.api.sent[-1][:2]))

    stub.api.sent.clear()
    stub.api.temp_sessions.clear()
    await stub._reply_command(temp_ev, [MessageChain().text("A"), MessageChain().text("B")])
    check("小窗的分段回复每段都带来源群",
          len(stub.api.sent) == 2 and len(stub.api.temp_sessions) == 2
          and all(g == str(GROUP_BOUND) for _u, g in stub.api.temp_sessions),
          str(stub.api.temp_sessions))

    class _WsStub:
        def __init__(self):
            self.payloads = []

        async def request(self, payload, timeout=30.0):
            self.payloads.append(payload)
            return ApiResponse(status="ok", retcode=0, data={"message_id": 1},
                               echo=payload.get("echo"))

    ws_stub = _WsStub()
    api_stub = ApiClient(ws_stub)
    await api_stub.send_private_msg("123", "hi", group_id="456")
    check("ApiClient.send_private_msg 透传 group_id",
          ws_stub.payloads[-1]["params"].get("group_id") == "456",
          str(ws_stub.payloads[-1]["params"]))
    await api_stub.send_private_msg("123", "hi")
    check("普通私聊不带 group_id", "group_id" not in ws_stub.payloads[-1]["params"],
          str(ws_stub.payloads[-1]["params"]))

    # 端到端：小窗里发命令 → 插件产出 → 框架按临时会话回复
    stub.api.sent.clear()
    stub.api.temp_sessions.clear()
    res_temp = await cmds.handle(temp_ev)
    await stub._reply_command(temp_ev, res_temp)
    check("小窗执行命令后回复带来源群且内容正常",
          stub.api.temp_sessions == [(str(SUPERUSER), str(GROUP_BOUND))]
          and "推送" in flat(res_temp),
          f"{stub.api.temp_sessions} {flat(res_temp)[:40]!r}")

    # 插件 preview：小窗里执行也要带来源群
    plugin._store = StateStore(str(TMP / "data" / "temp.json"),
                               str(TMP / "data" / "temp-notices.jsonl"), retention=50)
    _push(plugin, groups=[], users=[])
    plugin._portal = FakePortal([make_notice(Notice, "210001", "小窗预览通知")])
    api.sent.clear()
    await plugin._sync_once(reason="test", force_push=True,
                            preview_user=SUPERUSER, preview_group=GROUP_BOUND)
    check("preview 在小窗里发送时带来源群",
          bool(api.sent) and api.sent[-1][0] == "private"
          and api.temp_sessions
          and api.temp_sessions[-1] == (str(SUPERUSER), str(GROUP_BOUND)),
          str(api.temp_sessions) if api.sent else "no send")
    _push(plugin, groups=[GROUP_BOUND])

    # 参数校验失败：回执由框架产出（handle() 的返回值），路由由分发器 _reply_command 负责
    # ⚠️ 断言看 `stub.api`：回执走分发器（stub），不是插件自己的 api。
    stub.api.sent.clear()
    stub.api.temp_sessions.clear()
    bad_temp = MessageEvent(
        message_type="private", sub_type="group", user_id=SUPERUSER,
        group_id=GROUP_BOUND,
        message=[{"type": "text", "data": {"text": "/buptnews list abc"}}],
        raw_message="/buptnews list abc")
    res_bad_temp = await cmds.handle(bad_temp)
    check("小窗参数错误：框架返回「用法不正确 + 帮助页」",
          isinstance(res_bad_temp, str) and "用法不正确" in res_bad_temp
          and "/buptnews list" in res_bad_temp, flat(res_bad_temp)[:120])
    await stub._reply_command(bad_temp, res_bad_temp)
    check("小窗参数错误只回小窗、不发到群里",
          bool(stub.api.sent) and all(k == "private" for k, _t, _m in stub.api.sent)
          and stub.api.temp_sessions
          and stub.api.temp_sessions[-1][1] == str(GROUP_BOUND),
          f"sent={[(k, t) for k, t, _m in stub.api.sent]} temp={stub.api.temp_sessions}")

    stub.api.sent.clear()
    bad_group = MessageEvent(
        message_type="group", user_id=USER_ALLOWED, group_id=GROUP_BOUND,
        message=[{"type": "text", "data": {"text": "/buptnews list abc"}}],
        raw_message="/buptnews list abc")
    res_bad_group = await cmds.handle(bad_group)
    check("群里参数错误：同样回帮助页", isinstance(res_bad_group, str)
          and "用法不正确" in res_bad_group, flat(res_bad_group)[:120])
    await stub._reply_command(bad_group, res_bad_group)
    check("群里参数错误仍回群里",
          bool(stub.api.sent) and stub.api.sent[-1][0] == "group"
          and stub.api.sent[-1][1] == str(GROUP_BOUND),
          str([(k, t) for k, t, _m in stub.api.sent]))

    print("\n[9] on_disable 清理")
    await mgr.disable("bupt_news")
    check("停用后定时任务被移除", "bupt_news.sync" not in timer._metas, str(list(timer._metas)))
    check("停用后命令被移除", "buptnews" not in cmds.list_commands())

    print("\n[10] WebSocket 自动重连与 API 上报节流")
    await ws_tests(TMP)

    if online:
        print("\n[11] 在线联调（真实门户 / 天气 / 摘要 / 全链路）")
        await online_tests(Notice, plugin, api, cmds)

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


# ---------------------------------------------------------------- WebSocket 重连 / 上报节流

def _ws_reply(echo: str, data: dict = None, retcode: int = 0) -> str:
    return json.dumps({"status": "ok", "retcode": retcode,
                       "data": data or {}, "echo": echo}, ensure_ascii=False)


async def _wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return bool(pred())


async def _stop_all(*tasks):
    for t in tasks:
        if t is None or t.done():
            continue
        t.cancel()
        with contextlib.suppress(BaseException):
            await t


async def _serve(handler):
    """在回环地址起一个临时 WebSocket 服务，返回 (server, port)。"""
    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def ws_tests(tmp: Path) -> None:
    """WS 自动重连（任何报错都要重连）+ 所有 API 上报的 0.5–1s 节流。"""
    from napcat.client import WsHandler, parse_api_delay

    # ---- 默认节流区间与解析 ----
    default_ws = WsHandler("ws://127.0.0.1:1")
    check("API 上报默认延迟区间为 0.5–1.0s", default_ws.api_delay == (0.5, 1.0),
          str(default_ws.api_delay))
    samples = [default_ws._next_delay() for _ in range(50)]
    check("延迟随机值落在 0.5–1.0s 内", all(0.5 <= v <= 1.0 for v in samples),
          str(samples[:5]))
    check("api_delay 支持 None/数字/区间/单元素",
          parse_api_delay(None) == (0.5, 1.0) and parse_api_delay(0) == (0.0, 0.0)
          and parse_api_delay([0.2, 0.8]) == (0.2, 0.8)
          and parse_api_delay([0.3]) == (0.3, 0.3),
          f"{parse_api_delay(None)} {parse_api_delay(0)} "
          f"{parse_api_delay([0.2, 0.8])} {parse_api_delay([0.3])}")
    check("reconnect_interval 会被用于退避（并封顶）",
          WsHandler("ws://x", reconnect_interval=5.0)._retry_delay(1) == 5.0
          and WsHandler("ws://x", reconnect_interval=5.0)._retry_delay(9) == 60.0,
          str(WsHandler("ws://x", reconnect_interval=5.0)._retry_delay(9)))

    # ---- A. 服务端主动断开 → 自动重连，重连后仍能收发 ----
    conns: list = []

    async def handler_a(ws):
        conns.append(ws)
        first = len(conns) == 1
        try:
            async for raw in ws:
                data = json.loads(raw)
                await ws.send(_ws_reply(data.get("echo"), {"user_id": 42, "nickname": "t"}))
                if first:
                    await ws.close()          # 第一条请求应答后立刻断线
                    return
        except Exception:
            pass

    server_a, port_a = await _serve(handler_a)
    ws_a = WsHandler(f"ws://127.0.0.1:{port_a}", reconnect_interval=0.2, api_delay=(0, 0))
    ws_a._running = True
    rt_a = asyncio.create_task(ws_a.run_forever())
    pt_a = asyncio.create_task(ws_a._process_loop())
    try:
        check("首次连接建立", await _wait_until(lambda: ws_a.connect_count >= 1, 3.0),
              f"connect_count={ws_a.connect_count}")
        r1 = await ws_a.request({"action": "get_login_info", "echo": "a1"}, timeout=5)
        check("断线前请求可正常应答", r1.ok and r1.data.get("user_id") == 42, repr(r1))
        check("服务端断开后自动重连",
              await _wait_until(lambda: len(conns) >= 2, 5.0), f"connections={len(conns)}")
        r2 = await ws_a.request({"action": "get_login_info", "echo": "a2"}, timeout=5)
        check("重连后请求仍可应答", r2.ok and r2.data.get("user_id") == 42, repr(r2))
    finally:
        ws_a._running = False
        await _stop_all(rt_a, pt_a)
        server_a.close()
        await server_a.wait_closed()

    # ---- B. 首次连接失败（NapCat 还没起来）→ 后台重试，就绪后自动连上 ----
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()

    async def handler_idle(ws):
        with contextlib.suppress(Exception):
            async for _ in ws:
                pass

    ws_b = WsHandler(f"ws://127.0.0.1:{free_port}", reconnect_interval=0.2, api_delay=(0, 0))
    ws_b._running = True
    rt_b = asyncio.create_task(ws_b.run_forever())
    server_b = None
    try:
        await asyncio.sleep(0.4)              # 这段时间连接必然失败
        check("首次连接失败不中断、进入重试", ws_b.connect_count == 0,
              f"connect_count={ws_b.connect_count}")
        server_b = await websockets.serve(handler_idle, "127.0.0.1", free_port)
        check("服务器就绪后自动连上",
              await _wait_until(lambda: ws_b.connect_count >= 1, 5.0),
              f"connect_count={ws_b.connect_count}")
    finally:
        ws_b._running = False
        await _stop_all(rt_b)
        if server_b is not None:
            server_b.close()
            await server_b.wait_closed()

    # ---- C. 请求挂起时断线 → 立即失败，而不是白等 30s 超时 ----
    async def handler_c(ws):
        try:
            async for _ in ws:
                await ws.close()              # 收下请求但不回复，直接断开
                return
        except Exception:
            pass

    server_c, port_c = await _serve(handler_c)
    ws_c = WsHandler(f"ws://127.0.0.1:{port_c}", reconnect_interval=0.2, api_delay=(0, 0))
    ws_c._running = True
    rt_c = asyncio.create_task(ws_c.run_forever())
    pt_c = asyncio.create_task(ws_c._process_loop())
    try:
        await _wait_until(lambda: ws_c.connect_count >= 1, 3.0)
        t0 = time.monotonic()
        err = ""
        try:
            await ws_c.request({"action": "x", "echo": "c1"}, timeout=30)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        elapsed = time.monotonic() - t0
        check("断线时挂起请求立即失败（不等 30s 超时）",
              bool(err) and elapsed < 3.0, f"{err} elapsed={elapsed:.2f}s")
    finally:
        ws_c._running = False
        await _stop_all(rt_c, pt_c)
        server_c.close()
        await server_c.wait_closed()

    # ---- D. 每次上报前确实等待；并发上报被串行化 ----
    async def handler_d(ws):
        try:
            async for raw in ws:
                data = json.loads(raw)
                await ws.send(_ws_reply(data.get("echo")))
        except Exception:
            pass

    server_d, port_d = await _serve(handler_d)
    ws_d = WsHandler(f"ws://127.0.0.1:{port_d}", reconnect_interval=0.2, api_delay=(0.25, 0.25))
    ws_d._running = True
    rt_d = asyncio.create_task(ws_d.run_forever())
    pt_d = asyncio.create_task(ws_d._process_loop())
    try:
        await _wait_until(lambda: ws_d.connect_count >= 1, 3.0)
        t0 = time.monotonic()
        r = await ws_d.request({"action": "x", "echo": "d1"}, timeout=5)
        one = time.monotonic() - t0
        check("每次 API 上报前等待了配置的延迟", r.ok and one >= 0.25,
              f"elapsed={one:.2f}s")
        t0 = time.monotonic()
        await asyncio.gather(*[ws_d.request({"action": "x", "echo": f"d{i}"}, timeout=5)
                               for i in range(3)])
        three = time.monotonic() - t0
        check("并发上报被串行化（3 × 0.25s）", three >= 0.75, f"elapsed={three:.2f}s")
    finally:
        ws_d._running = False
        await _stop_all(rt_d, pt_d)
        server_d.close()
        await server_d.wait_closed()

    # ---- F. 掉线期间的 API 调用排队，重连后补发 ----
    probe_f = socket.socket()
    probe_f.bind(("127.0.0.1", 0))
    free_port_f = probe_f.getsockname()[1]
    probe_f.close()

    ws_f = WsHandler(f"ws://127.0.0.1:{free_port_f}", reconnect_interval=0.2,
                     api_delay=(0, 0), offline_hold_timeout=0)   # 0 = 一直等
    ws_f._running = True
    rt_f = asyncio.create_task(ws_f.run_forever())
    pt_f = asyncio.create_task(ws_f._process_loop())
    server_f = None
    try:
        await asyncio.sleep(0.3)                     # 服务器还没起，肯定掉线
        pending = asyncio.create_task(
            ws_f.request({"action": "get_login_info", "echo": "f1"}, timeout=10))
        await asyncio.sleep(0.4)
        check("掉线期间 API 调用不失败、排队等待",
              not pending.done() and ws_f._queued == 1,
              f"done={pending.done()} queued={ws_f._queued}")

        got_f = []

        async def handler_f(ws):
            got_f.append(ws)
            try:
                async for raw in ws:
                    d = json.loads(raw)
                    await ws.send(_ws_reply(d.get("echo"),
                                            {"user_id": 77, "nickname": "late"}))
            except Exception:
                pass

        server_f = await websockets.serve(handler_f, "127.0.0.1", free_port_f)
        r_f = await asyncio.wait_for(pending, timeout=8)
        check("服务器上线后排队调用被补发并拿到响应",
              r_f.ok and r_f.data.get("user_id") == 77, repr(r_f))
        check("补发后队列清空", ws_f._queued == 0, str(ws_f._queued))

        # 掉线期间多个调用一起排队，重连后按序补发
        server_f.close()
        await server_f.wait_closed()
        server_f = None
        await asyncio.sleep(0.4)
        batch = [asyncio.create_task(ws_f.request({"action": "x", "echo": f"b{i}"},
                                                  timeout=10))
                 for i in range(3)]
        await asyncio.sleep(0.4)
        check("多个调用同时排队", ws_f._queued == 3 and not any(t.done() for t in batch),
              f"queued={ws_f._queued}")
        server_f = await websockets.serve(handler_f, "127.0.0.1", free_port_f)
        rs = await asyncio.wait_for(asyncio.gather(*batch), timeout=8)
        check("重连后 3 个排队调用全部补发成功",
              all(r.ok for r in rs) and ws_f._queued == 0,
              f"ok={[r.ok for r in rs]} queued={ws_f._queued}")
    finally:
        ws_f._running = False
        await _stop_all(rt_f, pt_f)
        if server_f is not None:
            server_f.close()
            await server_f.wait_closed()

    # 队列上限 / 等待超时 / 关闭时不再挂住
    ws_cap = WsHandler("ws://127.0.0.1:1", reconnect_interval=0.2, api_delay=(0, 0),
                       offline_queue_max=1)
    ws_cap._running = True
    rt_cap = asyncio.create_task(ws_cap.run_forever())
    try:
        await asyncio.sleep(0.2)
        first_wait = asyncio.create_task(ws_cap.request({"action": "x", "echo": "c1"},
                                                        timeout=10))
        await asyncio.sleep(0.2)
        err_cap = ""
        try:
            await ws_cap.request({"action": "x", "echo": "c2"}, timeout=10)
        except Exception as e:  # noqa: BLE001
            err_cap = f"{type(e).__name__}: {e}"
        check("排队数超过上限时明确报错（不无限堆积）",
              "上限" in err_cap and ws_cap._queued == 1, err_cap or f"queued={ws_cap._queued}")
        first_wait.cancel()
        with contextlib.suppress(BaseException):
            await first_wait
    finally:
        ws_cap._running = False
        await _stop_all(rt_cap)

    ws_to = WsHandler("ws://127.0.0.1:1", reconnect_interval=0.2, api_delay=(0, 0),
                      offline_hold_timeout=0.3)
    ws_to._running = True
    rt_to = asyncio.create_task(ws_to.run_forever())
    try:
        await asyncio.sleep(0.15)
        t0 = time.monotonic()
        err_to = ""
        try:
            await ws_to.request({"action": "x", "echo": "t1"}, timeout=10)
        except Exception as e:  # noqa: BLE001
            err_to = f"{type(e).__name__}: {e}"
        elapsed_to = time.monotonic() - t0
        check("设置了等待上限时按上限失败（不再无限挂住）",
              "超时" in err_to and 0.2 <= elapsed_to < 3.0,
              f"{err_to} elapsed={elapsed_to:.2f}s")
    finally:
        ws_to._running = False
        await _stop_all(rt_to)

    ws_off = WsHandler("ws://127.0.0.1:1", reconnect_interval=0.2, api_delay=(0, 0),
                       offline_hold_timeout=0)
    ws_off._running = True
    rt_off = asyncio.create_task(ws_off.run_forever())
    await asyncio.sleep(0.2)
    waiter = asyncio.create_task(ws_off.request({"action": "x", "echo": "o1"}, timeout=10))
    await asyncio.sleep(0.2)
    ws_off._running = False                      # 模拟 stop()：排队调用应被取消而不是挂死
    err_off = ""
    try:
        await asyncio.wait_for(waiter, timeout=3)
    except Exception as e:  # noqa: BLE001
        err_off = f"{type(e).__name__}: {e}"
    check("关闭时排队调用被取消（不会永久挂住）",
          "已关闭" in err_off, err_off or "未抛错")
    await _stop_all(rt_off)

    # ---- E. NapCatClient 全链路：断线重连后刷新 self_id，并能干净退出 ----
    empty_plugins = tmp / "ws-plugins"
    empty_plugins.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp / "ws-client.json"
    cfg_path.write_text(json.dumps({"api_delay": [0, 0]}, ensure_ascii=False),
                        encoding="utf-8")

    conns_e: list = []
    logins = {"n": 0}

    async def handler_e(ws):
        conns_e.append(ws)
        try:
            async for raw in ws:
                data = json.loads(raw)
                payload = {}
                if data.get("action") == "get_login_info":
                    logins["n"] += 1
                    payload = {"user_id": 9000 + logins["n"],
                               "nickname": f"bot{logins['n']}"}
                await ws.send(_ws_reply(data.get("echo"), payload))
                if len(conns_e) == 1 and logins["n"] == 1:
                    await ws.close()          # 拿到登录信息后断开
                    return
        except Exception:
            pass

    server_e, port_e = await _serve(handler_e)
    from napcat.client import NapCatClient
    client = NapCatClient(ws_url=f"ws://127.0.0.1:{port_e}", config_path=str(cfg_path),
                          plugin_dir=str(empty_plugins), reconnect_interval=0.2,
                          api_delay=(0, 0))
    start_task = asyncio.create_task(client.start())
    try:
        ok = await _wait_until(lambda: client.self_id >= 9002 and len(conns_e) >= 2, 8.0)
        check("客户端断线后自动重连并刷新 self_id", ok,
              f"self_id={client.self_id} connections={len(conns_e)}")
    finally:
        await client.stop()
        with contextlib.suppress(BaseException):
            await start_task
        server_e.close()
        await server_e.wait_closed()
    check("关闭后 WebSocket 任务已清理",
          client._ws_task.done() and not client._ws._running,
          f"task_done={client._ws_task.done()} running={client._ws._running}")


async def online_tests(Notice, plugin=None, api=None, cmds=None):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bn_portal", str(PLUGIN_DIR / "portal.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bn_portal"] = mod      # Python 3.14 dataclass 需要模块已注册
    spec.loader.exec_module(mod)

    user = os.environ.get("BUPT_PORTAL_USERNAME", "")
    pwd = os.environ.get("BUPT_PORTAL_PASSWORD", "")
    check("提供了在线测试凭据", bool(user) and bool(pwd),
          "需要 BUPT_PORTAL_USERNAME / BUPT_PORTAL_PASSWORD")
    if not (user and pwd):
        return

    client = mod.PortalClient(username=user, password=pwd)
    StateStore = sys.modules["napcat_plugin_bupt_news.store"].StateStore
    wc = None
    summ = None
    try:
        ok = await client.login()
        check("CAS 登录成功", bool(ok))
        items = await client.fetch_list(1154)
        check("拉到通知列表", len(items) > 0, f"{len(items)} 条")
        if items:
            first = items[0]
            print(f"       最新：{first.date} | {first.dept} | {first.title[:36]}")
            check("列表条目字段完整",
                  bool(first.title) and bool(first.newsid) and bool(first.url))
            # 最新一条可能是纯图片通知（正文为空），多试几条再判定
            body_len = 0
            for cand in items[:5]:
                d = await client.fetch_detail(cand.url)
                body_len = len(d.get("body", ""))
                if body_len:
                    print(f"       正文 {body_len} 字：{d['body'][:60]}…")
                    break
            check("拉到正文详情（前 5 条中至少 1 条有文字）", body_len > 0,
                  "前 5 条正文均为空，可能都是图片类通知")

        # ---- 真实天气 ----
        wmod = sys.modules["napcat_plugin_bupt_news.weather"]
        wc = wmod.WeatherClient(logger=None)
        desc = await wc.today()
        check("真实 Open-Meteo 天气可用", bool(desc), "返回 None")
        if desc:
            print(f"       天气：{wmod.weather_line(desc)}")

        # ---- 真实 DeepSeek 批量摘要 ----
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        check("提供了 DeepSeek key", bool(key), "需要 DEEPSEEK_API_KEY")
        if key and items:
            smod = sys.modules["napcat_plugin_bupt_news.summarize"]
            summ = smod.Summarizer(api_key=key, batch_size=5, max_tokens=4096,
                                   logger=logging.getLogger("napcat.online"))
            sample = [{"newsid": "t1",
                       "title": "关于2026年秋季学期本科生转专业名单公布的通知"},
                      {"newsid": "t2", "title": "关于教学用途服务器集中纳管的通知"}]
            got = await summ.summarize(sample)
            check("真实摘要返回 2 条", len(got) == 2, str(got))
            check("真实摘要均不超 20 字",
                  bool(got) and all(len(v) <= 20 for v in got.values()), str(got))
            for k2, v in got.items():
                print(f"       摘要 {k2}: {v}（{len(v)} 字）")

            # ---- 全链路：真门户 + 真天气 + 真摘要，仅桩掉 QQ 发送 ----
            if plugin is not None and api is not None:
                plugin._portal = _PortalShim(client, items[:2])
                plugin._weather = wc
                plugin._summarizer = summ
                plugin._store = StateStore(str(TMP / "data" / "live.json"),
                                           str(TMP / "data" / "live-notices.jsonl"),
                                           retention=50)
                _push(plugin, groups=[], users=[99999999])   # 由 FakeApi 接住
                plugin.config.set("enabled", True)
                real_inq = plugin._in_quiet_hours
                plugin._in_quiet_hours = lambda now=None: False

                api.sent.clear()
                await plugin._sync_once(reason="online")   # 首轮：建基线 + 当天首次早报
                check("全链路首轮发出当天首次早报", len(api.sent) == 1, str(api.texts()))
                first_msg = api.texts()[-1][2] if api.sent else ""
                check("基线通知不推送，首轮只含天气行",
                      first_msg.startswith("☀️") and len(first_msg.splitlines()) == 1,
                      repr(first_msg))

                # 造一条“新通知”，走真实正文抓取 + 真实摘要
                fresh = Notice(column="1154", newsid="990000001", treeid="2149",
                               title="关于开展校园网络设备巡检的通知",
                               dept="数智化部", date="2026-09-24",
                               url=items[0].url)
                plugin._portal = _PortalShim(client, [fresh])
                plugin._store.note_digest("1999-01-01")       # 强制下一次为早报
                api.sent.clear()
                r1 = await plugin._sync_once(reason="online")
                check("全链路早报已发出", len(api.sent) == 1, f"r={r1}")
                if api.sent:
                    out = api.texts()[-1][2]
                    print("       —— 实际早报 ——")
                    for line in out.splitlines():
                        print("       " + line)
                    check("早报首行是天气行",
                          out.splitlines()[0].startswith("☀️"), out[:60])
                    check("早报含 #ID 摘要行",
                          any(ln[:1].isdigit() for ln in out.splitlines()[1:]),
                          out[:200])
                check("全链路已记录可查询通知", plugin._store.notice_count() >= 1,
                      str(plugin._store.notice_count()))
                rec1 = plugin._store.get_notice(1)
                check("库中 summary 字段已回填",
                      bool(rec1 and rec1.get("summary")), str(rec1))
                check("库中 summary 已截到 20 字以内",
                      rec1 and len(rec1.get("summary") or "") <= 20,
                      repr((rec1 or {}).get("summary")))
                plugin._in_quiet_hours = real_inq
    finally:
        await client.close()
        if wc is not None:
            await wc.close()
        if summ is not None:
            await summ.close()



class _PortalShim:
    """在线全链路用：复用已登录的 PortalClient，但强制返回指定列表。"""

    def __init__(self, client, notices):
        self._c = client
        self._n = notices
        self.username = getattr(client, "username", "")
        self.password = getattr(client, "password", "")

    async def fetch_list(self, column, pages=1):
        return list(self._n)

    async def fetch_detail(self, url):
        return await self._c.fetch_detail(url)

    async def close(self):
        pass


if __name__ == "__main__":
    try:
        rc = asyncio.run(main("--online" in sys.argv))
    except Exception:
        traceback.print_exc()
        rc = 1
    sys.exit(rc)
