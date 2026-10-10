# bupt_news —— 校内通知同步与推送

定时同步北邮校内通知（门户登录 + AI 摘要 + 表格/图片处理），按配置推送到指定群与私聊。

这是 [ncqqpy](https://github.com/<you>/ncqqpy) 的插件，**独立仓库**：
框架按 `plugin_dir`（默认 `./plugins`）扫描子目录加载插件，所以把它 clone 到
框架的 `plugins/` 下即可。

## 安装

```bash
cd /path/to/ncqqpy
git clone https://github.com/<you>/ncqqpy-plugin-bupt-news plugins/bupt_news
# 重新加载（QQ 内，超级用户 · 仅私聊）
#   /reload bupt_news
```

要求：框架 [ncqqpy](https://github.com/<you>/ncqqpy)（`napcat` 包需可导入）。

## 配置

插件被**构造**时（`on_load()` 之前）会把自己的**全部**配置项写进
`config.json` 的 `plugins.bupt_news` 段（只补缺键、不覆盖已有值、幂等），所以打开
文件就能看到所有开关和默认值。模板如下：

```json
{
  "data_dir": "",
  "interval": 600,
  "enabled": true,
  "columns": [
    1154
  ],
  "groups": [],
  "users": [],
  "max_push_per_round": 8,
  "first_run_backfill": 0,
  "quiet_hours": {
    "start": "23:00",
    "end": "07:00"
  },
  "notice_retention": 500,
  "digest_max_items": 40,
  "list_page_size": 20,
  "body_max_chars": 0,
  "weather": {
    "enabled": true,
    "label": "北京",
    "latitude": 39.9608,
    "longitude": 116.359,
    "timeout": 20,
    "umbrella_pop": 50
  },
  "summarize": {
    "enabled": true,
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key": "",
    "batch_size": 3,
    "max_tokens": 4096,
    "timeout": 90,
    "reasoning_effort": "none",
    "retry": 5,
    "retry_delay": 2.0,
    "retry_on_notice": 2,
    "retry_scan_max": 60,
    "retry_cooldown": 1800.0,
    "push_budget": 200.0,
    "retry_budget": 90.0,
    "retry_timeout": 120,
    "retry_batch_size": 6
  },
  "images": {
    "enabled": true,
    "max_bytes": 10485760,
    "prefetch": true,
    "prefetch_max": 30,
    "recrawl": true,
    "recrawl_max": 50,
    "retry": 2,
    "retry_delay": 2.0,
    "max_attempts": 5
  },
  "tables": {
    "enabled": true,
    "max_per_notice": 5
  },
  "chunk": {
    "max_chars": 1200,
    "max_images": 4
  },
  "binding": {
    "groups": [],
    "users": []
  },
  "username": "",
  "password": ""
}
```

环境变量（优先级高于文件）：

| 变量 | 说明 |
|------|------|
| `BUPT_PORTAL_USERNAME / BUPT_PORTAL_PASSWORD` | 门户账号（也可写在 `config.json → plugins.bupt_news.username/password`；**环境变量优先**） |
| `DEEPSEEK_API_KEY` | AI 摘要的 key（也可写进 `summarize.api_key`） |


详细文档：[`docs/bupt-news.md`](docs/bupt-news.md)。
## 测试

```bash
# 独立仓库里跑：框架需可导入（PYTHONPATH 指向框架仓库，或 pip install -e）
PYTHONPATH=/path/to/ncqqpy python -B tests/test_bupt_news.py
```

套件：`tests/test_bupt_news.py`。测试会在 `tests/.test-tmp/` 下建临时目录，不碰真实数据。

## 注意

- 维护脚本 `tests/backfill_images.py`、`tests/import_history.py` 会**写真实数据**，跑之前先停 bot 并确认 `--data-dir` 指向线上目录。
- `tests/diagnostics/probe_portal.py`、`probe_summarize.py` 是只读探针。

## License

MIT，见 [LICENSE](LICENSE)。
