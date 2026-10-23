# 十、测试与自检

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 上一章：[与 Node 爬虫的关系](09-node-crawler.md)


自检脚本与诊断探针都在 `tests/` 下（见 (01-layout.md#12-配套脚本与入口)）：

```bash
# 离线：解析 / 状态 / ID / 免打扰 / 早报 / 摘要 / 命令分发（用夹具与桩件，不联网）
venv-arm/bin/python -u tests/test_bupt_news.py

# 在线：真实门户 + 真实 Open-Meteo + 真实 DeepSeek + 全链路早报
BUPT_PORTAL_USERNAME=... BUPT_PORTAL_PASSWORD=... DEEPSEEK_API_KEY=... \
  venv-arm/bin/python -u tests/test_bupt_news.py --online

# 只诊断门户链路（带每步耗时）
BUPT_PORTAL_USERNAME=... BUPT_PORTAL_PASSWORD=... \
  venv-arm/bin/python -u tests/diagnostics/probe_portal.py
```
