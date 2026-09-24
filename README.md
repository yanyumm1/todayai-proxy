# today.ai (国际版) -> OpenAI 兼容反代 (Cloudflare Workers)

把 Today AI（today.ai 国际版）私有协议包装成 OpenAI 标准接口，任何兼容客户端直连。

## ✅ 状态：协议已实测跑通

| 接口 | 实测 |
|---|---|
| `POST /v1/messages` `{"content","role":"user"}` | ✅ 202，返回 threadId |
| `GET /v1/messages?threadId=` | ✅ 轮询拿 assistant 回复 |
| `POST today.ai/api/token`（带 session cookie） | ✅ 换 1h 有效 API JWT |
| 响应示例 | "我是豆皮，nian 的专属助手..." |

## 架构

```
[任意 OpenAI 客户端]
   ↓ /v1/chat/completions (Bearer GATEWAY_API_KEY)
[CF Worker]
   ↓ 自动用 session cookie 换 API token (1h, 内存缓存+临期续期)
   ↓ POST /v1/messages → 轮询 GET /v1/messages?threadId → 转 OpenAI SSE
[api.today.ai]
```

## 部署（2 分钟）

```bash
cd todayai-proxy
wrangler login

# 1. 抓 session cookie（见下）
# 2. 配置 secrets
wrangler secret put TODAY_SESSION_COOKIE   # 粘贴完整 cookie
wrangler secret put GATEWAY_API_KEY        # 自定义网关密钥

wrangler deploy
```

### 抓 session cookie

1. 浏览器登录 https://today.ai（Google 登录）
2. F12 → Application → Cookies → `today.ai`
3. 复制 `__Secure-better-auth.session_token` 的 **Name=Value**（连同其他 cookie 一起更稳）
   或 F12 → Network → 任意请求 → Request Headers → 整段 `Cookie:` 值

> cookie 是长效凭证（Better Auth session），Worker 每次自动换短效 API token，**不用每小时手动更新**。
> session 过期后（一般 7 天+）重新抓一次即可。

### 客户端接入

```
Base URL : https://todayai-proxy.<你的子域>.workers.dev/v1
API Key  : 你的 GATEWAY_API_KEY
Model    : today-balanced / today-fast / today-power
```

## curl 测试

```bash
curl -N https://todayai-proxy.<子域>.workers.dev/v1/chat/completions \
  -H "Authorization: Bearer <GATEWAY_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"today-balanced","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 逆向事实存档

- 认证: `auth.today.ai` Better Auth（国际版邮箱 OTP 可用；发码 `POST /api/auth/email-otp/send-verification-otp {email,type:"sign-in"}`，验证 `POST /api/auth/sign-in/email-otp {email,otp}`）
- API 换票: `POST today.ai/api/token` `{audience:"https://api.today.ai",expiresIn:3600}`（需 session cookie，Bearer session token 无效）
- API: `api.today.ai/v1/*`，JWT（issuer=auth.today.ai, aud=api.today.ai, 1h）
- 模型选择: 仅 `mode.fast / mode.balanced / mode.power` 三档（底层模型不公开，助手自称"豆皮"）
- 定价: Pro $20/月 · Ultra $200/月
- 其他端点: `/v1/agents/default`、`/v1/automations`、`/v1/memories/overview`、`/v1/skills`

## 文件

| 文件 | 说明 |
|---|---|
| `worker.js` | CF Workers 版（协议已实测校准） |
| `wrangler.toml` | Worker 配置 |
| `todayai_cookie.py` | 一键提取 today.ai session cookie（可选自动写入 Worker secret） |
| `update_worker_cookie.py` | 全自动版：提取 cookie + 更新 Worker + 网关验证 |
| `README.md` | 本文档 |
