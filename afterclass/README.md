# 第二课堂：怎么提供凭据

这个目录放的是「北邮第二课堂」小程序的反编译 API 清单（`API.md` / `api-endpoints.json` / `api.http`）。

**本插件不实现登录**：账号密码登录在需要人机校验时返回 419/420，验证码页
`/mp-captcha.html` 是商业反爬控件（瑞数 `$_ts` 混淆）。绕过它属于规避访问控制，
所以代码只接受你自己提供的 JWT。

## 取 token

**网页版（最省事）**：浏览器打开 <https://dekt.bupt.edu.cn/>，登录后 `F12` → Console：

```js
localStorage["secondclass.tokenv3"]
```

打印出的 `"eyJ..."` 就是 JWT（带引号复制也没关系，程序会清洗）。

**小程序**：开发者工具 → Storage → 键 `tokenv3`（同一个值）。

JWT 里 `sub` 是用户 id（接口路径要用它），`exp` 是过期时间。

## 放进这个目录

```bash
echo '<JWT>' > plugins/bupt_news/afterclass/token.txt
```

也可以不走文件，用环境变量 `DEKT_TOKEN`（优先级更高）。

⚠️ `token.txt` 含登录凭据：不要提交到仓库，不要贴到聊天记录里。

## 验证

```bash
./venv-arm/bin/python -B tools/dekt_probe.py
```

先把 `config.json` 的 `plugins.bupt_news.afterclass.enabled` 设为 `true`，
同步就会在每轮通知同步之后跑一次。

## 没有真账号也能联调

[`final_draft.md`](final_draft.md) 是对小程序的行为分析报告。依据它实现的**离线
模拟器**在**框架仓库**的 `tools/dekt_sim/`（不在本插件仓库里）：43 条内存路由 +
三种编码算法 + 18 个场景，不发真实请求、不碰生产。

```bash
./venv-arm/bin/python -B -m tools.dekt_sim scenarios   # 只跑场景
./venv-arm/bin/python -B -m tools.dekt_sim serve       # 起本地假后端（127.0.0.1）
./venv-arm/bin/python -B tests/test_dekt_sim.py        # 自检 79 项
```

用它验证的是**客户端的走法**（拉活动 → 归一化 → 报名/签到规则），不能替代真实
服务端的鉴权与名额规则 —— 那些仍需授权环境里复核。
