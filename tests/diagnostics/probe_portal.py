#!/usr/bin/env python
"""在线诊断：逐步验证 CAS 登录 → 列表 → 正文（含图片）→ 图片下载，每步带超时与耗时。"""
import asyncio
import importlib.util
import os
import socket
import sys
import time
from pathlib import Path

# 本文件在 tests/ 下 → 仓库根是上一级（脚本原来放在仓库根）。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "bn_portal", str(ROOT / "plugins" / "bupt_news" / "portal.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["bn_portal"] = mod          # Python 3.14 dataclass 需要模块已注册
spec.loader.exec_module(mod)


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


async def main():
    log("DNS:")
    for h in ("my.bupt.edu.cn", "auth.bupt.edu.cn"):
        try:
            log(h, "->", socket.getaddrinfo(h, None)[0][4][0])
        except Exception as e:
            log(h, "FAIL", e)

    user = os.environ.get("BUPT_PORTAL_USERNAME", "")
    pwd = os.environ.get("BUPT_PORTAL_PASSWORD", "")
    log("credentials:", "yes" if (user and pwd) else "MISSING")

    c = mod.PortalClient(username=user, password=pwd, timeout=20)
    log("step 1: login ...")
    t = time.time()
    try:
        ok = await asyncio.wait_for(c.login(), timeout=45)
        log(f"  login OK={ok}  ({time.time()-t:.1f}s)")
    except asyncio.TimeoutError:
        log("  login TIMEOUT after 45s")
        await c.close()
        return
    except Exception as e:
        log(f"  login FAILED {type(e).__name__}: {e}")
        await c.close()
        return

    log("step 2: fetch_list(1154) ...")
    t = time.time()
    try:
        items = await asyncio.wait_for(c.fetch_list(1154), timeout=45)
        log(f"  got {len(items)} items ({time.time()-t:.1f}s)")
        for n in items[:3]:
            log(f"    {n.date} | {n.dept} | {n.title[:40]}")
    except Exception as e:
        log(f"  fetch_list FAILED {type(e).__name__}: {e}")
        await c.close()
        return

    if items:
        log("step 3: fetch_detail(...) ...")
        t = time.time()
        try:
            d = await asyncio.wait_for(c.fetch_detail(items[0].url), timeout=45)
            log(f"  detail OK ({time.time()-t:.1f}s) body={len(d['body'])} chars")
            log(f"    title={d['title'][:40]} dept={d['dept']} date={d['date']}")
            log("    body:", d["body"][:160].replace("\n", " / "))
        except Exception as e:
            log(f"  fetch_detail FAILED {type(e).__name__}: {e}")
            d = None

        imgs = (d or {}).get("images") or []
        if d is not None:
            log(f"    images={len(imgs)}")
            for it in imgs:
                log(f"      pos={int(it.get('pos', 0)):5d} {it['url'][:100]}")
        for it in imgs[:2]:
            log("step 4: fetch_bytes(图片) ...")
            t = time.time()
            try:
                data, ctype = await asyncio.wait_for(
                    c.fetch_bytes(it["url"]), timeout=30)
                log(f"  image OK ({time.time()-t:.1f}s) {len(data)} bytes "
                    f"ctype={ctype or '-'} magic={data[:8].hex()}")
            except Exception as e:
                log(f"  image FAILED {type(e).__name__}: {e}")

    await c.close()
    log("done")


asyncio.run(main())
