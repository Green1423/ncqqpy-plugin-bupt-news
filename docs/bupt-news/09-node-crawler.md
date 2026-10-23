# 九、与 Node 爬虫的关系

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 上一章：[排障](08-troubleshooting.md) · 下一章：[测试与自检](10-tests.md)


另有一套**独立的离线批量爬取**工具（`crawl.js` / `login.js`，不在本仓库内，
导出 JSONL + 附件）。本插件是**在线增量推送**，两者互相独立：

- 批量归档、要附件 → 用 `crawl.js` + `download-attachments.js`
- 实时通知、要推送 → 用本插件

两者共用同一套站点知识：CAS 登录流程、`list.jsp` 的 `totalpage` 陷阱、
`.v_news_content` 正文容器、纯图片通知正文为空等。

---
