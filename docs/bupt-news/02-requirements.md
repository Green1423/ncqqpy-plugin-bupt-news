# 二、运行前提

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 上一章：[目录结构](01-layout.md) · 下一章：[配置](03-config.md)


1. **NapCatQQ 必须已运行**，并开启了 WebSocket 服务端，地址与 `config.json` 的
   `ws_url`（默认 `ws://localhost:3001`）一致。
   > `NapCatClient.start()` 会**先连 WebSocket，再加载插件**。所以 NapCat 没起来时，
   > 进程会在 `ws.connect()` 直接报错退出，插件不会被加载。
2. **Python 依赖**：使用本仓库的 `venv-arm`。
   > ⚠️ 原有的 `venv/` 是 **x86-64** 的（从别的机器拷来的），在 aarch64 上无法执行，
   > 且没有可执行位。请勿使用；用 `venv-arm`。

   重建方式：
   ```bash
   python3 -m venv --without-pip venv-arm
   venv-arm/bin/python get-pip.py
   venv-arm/bin/pip install websockets pydantic aiohttp aiofiles
   ```

3. **门户账号**（统一身份认证）——见 (03-config.md#32-凭据优先级)。

---
