# 北邮「第二课堂」小程序 API 清单

> 提取来源：反编译产物 `app-service.js` 中的 `api/*.js`、`utils/fly*.js`、`utils/code.js`、`bus.js` 及页面调用点。
> 共 **40 个 REST 接口**（`/api/v1/*`）+ 3 个辅助接口（配置、统计、图片）+ 1 套 H5 桥接。

---

## 1. 基础信息

| 项 | 值 |
| --- | --- |
| 生产环境 Base URL | `https://dekt.bupt.edu.cn` |
| 测试环境 Base URL | `https://dekt.241919810.xyz` |
| 统一前缀 | `/api/v1` |
| 请求库 | fly.js + 自研 engine（`wx.request` 封装），`utils/fly.js` |
| 鉴权方式 | `Authorization: Bearer <JWT>`，JWT 存 Storage `tokenv3` |
| Base URL 覆盖 | Storage `apibase`；关于页暗门（依次点三处）可切 Default / Staging / 自定义 |
| 小程序版本头 | `X-UApp-Version: <version 或 envVersion>` |
| 会话 Cookie | `Cookie: _rab=<v>; _vab=<v>`（从响应的 `set-cookie` 取，存 Storage `cookie._rab` / `cookie._vab`） |

### 公共参数 / 行为

- **`{currentUserId}` 路径占位符**：请求拦截器会用 JWT 的 `sub` 替换，例如 `GET /api/v1/user/{currentUserId}` → `/api/v1/user/12345`。
- **`_` 查询参数**：所有带鉴权的请求都会追加 `_=<timestamp>`，engine 再把设备号（base36）插到 `_=` 后面，实际形如 `_=<deviceId36><ts>`。
- **重试**：GET 遇到 5xx（或 status<200、网络错误）最多重试 2 次，追加 `_fly_retry=n`，超时每次 +2s；GET 超时 2s，全局默认 5s。
- **超时/错误提示**：`status=1` 表示超时，其余为网络错误；`metaIgnoreErr` 可忽略指定 HTTP 状态。
- **服务端业务状态**：响应头 `x-real-status` ≥ 400 时按错误处理；链路追踪头 `x-dekt-requestid`。
- **响应包装**：`{ data, digest?, msg?, code? }`，多数列表接口还会返回与 `data` 等长的 `digest` 数组，用于后续写操作校验。
- **特殊状态码**：登录 `419/420` → 需要图形验证码（响应 `data.data` 是 captcha token）；微信登录 `404` → openid 未绑定。

---

## 2. 认证 `api/auth.js`

| # | 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | POST | `/api/v1/auth/sessions` | form: `username` `password` `code` `captcha` | 账号密码登录。`code` = `wx.login()` 的 code，`captcha` = 图形验证码答案。成功返回 `{data:"<JWT>"}` |
| 2 | POST | `/api/v1/auth/wechat/sessions` | form: `code` | 微信一键登录，`code` = `wx.login()`；404 = 未绑定 |
| 3 | DELETE | `/api/v1/auth/wechat` | — | 解绑当前微信 |

**图形验证码流程**：登录返回 419/420 时，用 `data.data` 拼 `https://dekt.bupt.edu.cn/mp-captcha.html#<token>` 在 web-view 打开，页面回传答案后带 `captcha` 重试登录。

```bash
curl -X POST 'https://dekt.bupt.edu.cn/api/v1/auth/sessions' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'username=2021xxxxxx' \
  --data-urlencode 'password=******' \
  --data-urlencode 'code=' \
  --data-urlencode 'captcha='
```

---

## 3. 用户 `api/user.js`

| # | 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- | --- |
| 4 | GET | `/api/v1/user/{currentUserId}` | — | 当前用户资料；返回 `data`（用户对象）+ `digest`（用户摘要，报名/列表要用） |
| 5 | PUT | `/api/v1/user/{currentUserId}` | form: `email` `phone` | 修改资料 |

---

## 4. 组织与角色 `api/roles.js`

| # | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 6 | GET | `/api/v1/role/normal` | 普通角色列表（`[{id,name}]`） |
| 7 | GET | `/api/v1/group/org-college` | 学院/组织树（`id → name`） |
| 8 | GET | `/api/v1/group/societies` | 社团列表 |

---

## 5. 活动与报名 `api/activity.js`

| # | 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- | --- |
| 9 | GET | `/api/v1/activity` | query: `college_id` `class_id` `grade` `role_id` `digest` `is_group` | 活动列表，`digest` 传用户 digest；`is_group=1` 拉组织活动 |
| 10 | GET | `/api/v1/activity/{id}` | — | 活动详情 |
| 11 | GET | `/api/v1/activity/class` | — | 活动分类 |
| 12 | POST | `/api/v1/participation/{act_id}` | form: `digest` `start_t_ms` `end_t_ms` `target_id` `target_role` `grade` `customer` `demands` | **报名**。两个时间取活动报名起止 |
| 13 | DELETE | `/api/v1/participation/{act_id}` | query: `digest` | **取消报名**（fly 把 DELETE 的 data 拼进 query） |
| 14 | GET | `/api/v1/participation/acts` | query: `page`（1 起） `page_size`（默认 40） | 我参与的活动 |
| 15 | GET | `/api/v1/participation/admin/act` | query: `act_state` `group_as_admin`（-1 不传，0/1）+ 扩展字段 | 我管理的活动，`act_state` 为状态页签 |
| 16 | GET | `/api/v1/participation/admin/act/{act_id}` | query: `identity` `digest` | 活动管理员列表 |
| 17 | POST | `/api/v1/participation/admin/act/{act_id}` | form: `identity` `digest` `new_user_id` `new_identity` | 添加管理员 |
| 18 | DELETE | `/api/v1/participation/admin/act/{act_id}` | query: `identity` `digest` `delete_user_id` | 删除管理员 |
| 19 | PUT | `/api/v1/participation/admin/act/{act_id}` | form: `digest` `identity` | **结束活动** |
| 20 | GET | `/api/v1/preference` | — | 我的订阅设置 |
| 21 | PUT | `/api/v1/preference` | JSON body（设置对象） | 更新订阅设置 |
| 22 | POST | `/api/v1/subscription/{activity_id}` | query: `college_id` `grade` `role_id`，空 body | 订阅活动通知 |
| 23 | DELETE | `/api/v1/subscription/{activity_id}` | query: `college_id` `grade` `role_id` | 取消订阅 |

> `identity` 是管理身份（管理列表里每条活动的 `identity` 字段）；`digest` 是服务端下发的活动摘要，用于写操作防重放/乐观锁。

---

## 6. 签到 `api/checkin.js`

| # | 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- | --- |
| 24 | GET | `/api/v1/checkin/qrcode/user` | header: `X-Trace: <trace>` | 用户签到码载荷（服务端下发，前端 `encCode` 编码成二维码） |
| 25 | GET | `/api/v1/checkin/status` | query: `trace` | 轮询是否被扫，`data.msg === "Success"` 即成功 |
| 26 | POST | `/api/v1/checkin/passive` | form: 扫码解出的对象；header `X-Token: Base64(JSON(签到码对象))` | **管理员扫用户码签到** |
| 27 | POST | `/api/v1/checkin/ble/{packet}` | form: 签到码对象 | BLE 广播签到，`packet` 为广播包 hex |
| 28 | GET | `/api/v1/checkin/qrcode/admin/{act_id}/{opr_type}` | — | 管理员动态签到码，`opr_type`：0 签到 / 1 签退 |
| 29 | GET | `/api/v1/checkin/dynamic_list` | query: `act_id` `opr_type` `limit` `last_id` | 签到动态列表（增量拉取，`last_id` 传当前列表首条 id） |

**签到闭环**
1. 用户端：`GET /checkin/qrcode/user`（带 trace）→ 拿到载荷 → 本地编码二维码；同时每 2s 轮询 `GET /checkin/status?trace=`。
2. 管理员端：`GET /checkin/qrcode/admin/{act_id}/{opr_type}` 出码，用户扫码签到。
3. 管理员扫用户码：用 `utils/code.js` 的 `decCode()` 解出 `{iat,exp,sub,act_id,opr_type,act_demand,digest,trace}` → `GET /checkin/qrcode/user` 换新载荷 → `POST /checkin/passive`。
4. BLE：监听广播 → `GET /checkin/qrcode/user` 取载荷 → `POST /checkin/ble/{hex}`。

---

## 7. 通知 / 新闻 `api/news-new.js`

| # | 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- | --- |
| 30 | GET | `/api/v1/news/preview` | — | 首页预览 |
| 31 | GET | `/api/v1/news/tree` | — | 栏目树（本地缓存 `info-tree-id` / `info-tree-id-single`） |
| 32 | POST | `/api/v1/news/search` | JSON: `tree_id` `time:{from,to}` `query` `size` `offset` `type` | 搜索。`type`：`all` / `news` / `notification` / `announcement`；返回 `data.data.hits`、`data.data.total`，`data` 可能是字符串需二次 `JSON.parse` |

新闻类型枚举：`NEWS=0`、`NOTIFICATION=1`、`ANNOUNCEMENT=2`、`ALL=3`。

---

## 8. 积分商城 `api/store.js`

| # | 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- | --- |
| 33 | GET | `/api/v1/store/product` | query: `status` `detail` `target_id` | 商品列表（默认 0/0/-1） |
| 34 | GET | `/api/v1/store/product/{id}` | — | 商品详情 |
| 35 | POST | `/api/v1/store/order` | JSON: `product_id` `count` `redeem_type` | 下单兑换（纯积分，无支付） |
| 36 | GET | `/api/v1/store/orderRecords` | query: `page` `limit` `filter` | 兑换记录 |
| 37 | GET | `/api/v1/store/scoreRecords` | query: `page` `limit` | 积分流水 |
| 38 | GET | `/api/v1/store/score` | — | 我的积分：`{college_score, school_score}` |

---

## 9. 成绩单 PDF `api/transcript.js`

| # | 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- | --- |
| 39 | GET | `/api/v1/transcript/pdf/generate-jobs` | header: `Content-Type: application/json` | 生成任务列表：`data.result = {waiting[], active[], completed[], failed[]}` |
| 40 | POST | `/api/v1/transcript/pdf/generate` | JSON: `email` `semesters[]` `types[]` | 提交生成任务（邮件发送） |

`types`：1 思想成长、2 社会实践、3 志愿服务、4 学术科创、5 文化艺术、6 其他、40 强身健体。
`semesters` 形如 `"2024-2025-1"`。

---

## 10. 辅助接口（非 `/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/config/index.json` | 远程功能开关，相对 Base URL：`{notice, security:{disableLogout}, features:{urls:{enable,data[]}}}` |
| POST | `/api/v1/event/send?_=<deviceId36>` | 访问统计（自建 umami）：JSON `{type:"event", payload:{...}}`，头 `X-UApp-Version`、`x-umami-cache` |
| GET | `/api/v1/image/{imageId}` | 附件/图片直链（`getAttachmentLink()` 只拼 URL，不带鉴权头） |

---

## 11. H5 桥接（web-view）

小程序把 JWT 通过 URL hash 传给 H5：

```
{base}{path}#{"env":"mpweixin","theme":"chino","token":"<JWT>","prepare":{...},"_":<ts>}
```

| 入口 | 说明 |
| --- | --- |
| `/auth/token-land?t=<base36 分钟时间戳>` | 管理后台免登（`prepare` 带 `user`、`userDigest`、`actClass`） |
| `/mp-captcha.html#<captchaToken>` | 图形验证码页 |
| `/news/{id}` | 新闻详情 |
| `/qr?activity={id}` / `/qr?url={path}` / `/qr?checkin_type=code\|ble` | 扫码落地参数 |
| `/activity/{act_id}/checkin/scan?digest=&identity=&name=&forceOpenCamera=true` | 管理员扫码签到页 |
| `/apps/egg` | 关于页“Egg”调试入口 |

二维码域名：`dekt.bupt.edu.cn/qr`；签到码编码规则在 `utils/code.js`（`encCode` / `decCode`，时间基准常量 `1630425600` 即 2021-09-01）。

---

## 12. 枚举速查

| 枚举 | 值 |
| --- | --- |
| 活动区域 `EActivityArea` | 西土城 0 / 沙河 1 / 线上 2 / 海南 3 / 其他 100000000 |
| 受众级别 `CustomerLevel` | Campus 0 / CampusGrade 1 / School 2 / SchoolGrade 3 / Class 4 / Associations 5 / CustomGroup 6 |
| 活动能力位 `Demands` | 报名 1 / 签到 2 / 签退 4 / 活动频道 8 / 管理 16 / 隐藏 32 / 角色过滤 64 |
| 商品状态 `EProductStatus` | 在售 0 / 售罄 1 / 下架 2 |
| 兑换类型 `ERedeemType` | 仅学院积分 1 / 仅校级积分 2 / 不限 3 |
| 封禁类型 `EBannedType` | 报名 1 / 签到 2 / 兑换 4 |
| 积分变动 `ScoreChangeType` | 1 参与活动 / -1 兑换商品 / -2 活动未签到 |

---

## 13. 复现要点

1. 先 `POST /auth/wechat/sessions`（或账号密码）拿 JWT，后续所有请求带 `Authorization: Bearer <JWT>`。
2. 启动时固定拉 `GET /user/{sub}` 拿 `digest`，再 `GET /activity?digest=<userDigest>&...`——不带 digest 可能被拒。
3. 写操作（报名/管理/结束）都必须带服务端下发的 `digest`，且与服务端当前状态一致。
4. `GET` 有 2s 超时 + 2 次重试，抓包复现时注意 `_`、`_fly_retry` 参数干扰。
5. 签到接口的 `X-Token` 是**标准 Base64**（`btoa(JSON.stringify(payload))`），不是 JWT。

配套文件：`api-endpoints.json`（机器可读）、`api.http`（可直接跑的请求集）。
