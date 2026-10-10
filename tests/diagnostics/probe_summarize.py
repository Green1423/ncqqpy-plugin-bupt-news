#!/usr/bin/env python
"""设计探针（一次性）：确定摘要接口的调用形状。

要回答的设计问题：
  1. 逐条调用 vs 批量调用，成本/延迟差多少？
  2. 批量时模型能否稳定返回结构化 JSON？
  3. 需要多少 max_tokens 才不会因 reasoning 吃光预算而返回空 content？

只读、可丢弃，不构成任何长期 owner。
"""
import json
import os
import re
import time
import urllib.request

# API key 从环境变量取；也可以用 DSH_CREDENTIALS 指一个含 `DEEPSEEK_API_KEY: ...`
# 的文件（**不要**在脚本里写死个人路径：那既是隐私，也让别人没法直接用）。
KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
if not KEY:
    _creds = os.environ.get("DSH_CREDENTIALS", "").strip()
    if _creds and os.path.exists(_creds):
        _m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", open(_creds).read())
        KEY = _m.group(1) if _m else ""
if not KEY:
    raise SystemExit("请先设置 DEEPSEEK_API_KEY（或 DSH_CREDENTIALS 指向凭据文件）再运行本探针")
BASE = "https://api.deepseek.com"
MODEL = "deepseek-flash"

TITLES = [
    ("142431", "关于2026年9月批次授予普通全日制学生学士学位的通知", "教务部"),
    ("142426", "关于公布2026年秋季学期本科生转专业名单的通知", "教务部"),
    ("142423", "关于教学用途服务器集中纳管的通知", "数智化部"),
    ("142418", "关于公布2026-2027学年第一学期高年级本科生部分停开课程及补选选课安排的通知", "教务部"),
    ("142411", "关于启动我校2026-2027学年秋季学期研究生助教、助管工作并报送相关材料的通知", "学生工作部"),
    ("142408", "未来学院赴杭州开展科技前沿与产教融合调研", "未来学院"),
]


def call(body: dict) -> tuple[dict, float]:
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    return out, time.time() - t0


def usage(d: dict) -> str:
    u = d.get("usage", {})
    return (f"prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} "
            f"reasoning={u.get('completion_tokens_details', {}).get('reasoning_tokens')}")


def main():
    # ---- 探针 1：逐条调用（取前 3 条做样本）----
    print("=== 探针 1：逐条调用（3 条样本）===")
    tot_tok = 0
    t_all = time.time()
    for nid, title, dept in TITLES[:3]:
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content":
                          f"用不超过20个汉字总结这条校园通知的要点，只输出摘要本身："
                          f"{title}（{dept}）"}],
            "max_tokens": 1024,
        }
        d, dt = call(body)
        c = d["choices"][0]
        tot_tok += d.get("usage", {}).get("completion_tokens", 0) or 0
        print(f"  [{nid}] {dt:.1f}s  finish={c.get('finish_reason')}  "
              f"content={c['message'].get('content')!r}")
    print(f"  合计 {time.time()-t_all:.1f}s，completion tokens={tot_tok}")

    # ---- 探针 2：一次批量调用 ----
    print("\n=== 探针 2：一次批量（6 条）===")
    listing = "\n".join(f"{nid}. {t}（{d}）" for nid, t, d in TITLES)
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content":
             "你是校园通知摘要助手。对每条通知输出不超过20个汉字的要点摘要。"
             '只输出 JSON 数组，形如 [{"id":"142431","s":"摘要"}]，不要任何其他文字。'},
            {"role": "user", "content": f"请总结以下通知：\n{listing}"},
        ],
        "max_tokens": 3072,
    }
    d, dt = call(body)
    c = d["choices"][0]
    content = c["message"].get("content") or ""
    print(f"  {dt:.1f}s  finish={c.get('finish_reason')}  {usage(d)}")
    print(f"  原始输出: {content[:400]!r}")
    try:
        cleaned = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
        arr = json.loads(cleaned)
        print(f"  ✅ JSON 解析成功，{len(arr)} 条：")
        for it in arr:
            print(f"     {it.get('id')}: {it.get('s')!r}  ({len(it.get('s',''))} 字)")
    except Exception as e:
        print(f"  ❌ JSON 解析失败: {e}")


if __name__ == "__main__":
    main()
