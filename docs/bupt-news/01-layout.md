# 一、目录结构

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 下一章：[运行前提](02-requirements.md)


## 1.1 插件目录

```text
plugins/bupt_news/
├── __init__.py     # 导出 BuptNewsPlugin（框架按此发现插件）
├── main.py         # 插件主体：定时同步、免打扰/早报编排、推送、命令注册
├── portal.py       # 门户客户端：CAS 统一身份认证 + 列表/正文（含图片）抓取与解析
├── images.py       # 正文图片下载 + 本地缓存（按 URL 去重、自动清理）
├── backfill.py     # 图片补齐：历史记录重抓正文页 + 下载缺失图片
├── weather.py      # Open-Meteo 天气客户端 + WMO 天气码→中文映射
├── summarize.py    # DeepSeek（OpenAI 兼容）批量摘要客户端
├── schemas.py      # 命令参数 Schema
├── store.py        # 状态持久化：state.json（小状态）+ notices.jsonl（可检索正文）
└── data/           # 运行时生成：state.json、notices.jsonl、cookies.json、images/（图片缓存）
```

## 1.2 配套脚本与入口

配套文件（不在插件目录内）——自检脚本、诊断探针与一次性维护脚本都在 **`tests/`** 下，仓库根只留运行入口（`main.py` / `run.sh` / `config.json` / `napcat/` / `plugins/`）：

| 文件 | 说明 |
|------|------|
| (../../../tests/test_bupt_news.py) | 自检脚本（离线 **490+ 项**：解析 / 状态 / ID / 免打扰 / 早报 / 摘要 / 命令 / 权限与 `/help` / **私聊可用范围** / 图片排版 / 长消息分段 / WS 重连与上报节流；`--online` 追加在线联调）|
| (../../../tests/diagnostics/probe_portal.py) | 在线诊断：分步验证 登录 → 列表 → 正文，带每步耗时 |
| (../../../tests/diagnostics/probe_summarize.py) | 摘要接口调用形状探针（逐条 vs 批量、token 预算）|
| (../../../tests/backfill_images.py) | 一次性补齐存量通知的正文图片（重抓 + 下载，自动备份 notices.jsonl）|
| (../../../run.sh) | 启动脚本（自动用 `venv-arm`，并从 `.env` 读取密码）|
| `.env.example` | 环境变量模板（仓库暂未提供，可自行创建：门户密码 + DeepSeek key）|

---
