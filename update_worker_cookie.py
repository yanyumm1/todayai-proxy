#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_worker_cookie.py — 一键刷新 today.ai 登录 cookie 到 Cloudflare Worker
============================================================================
流程: OTP 发送验证码 → 输入/自动抓取验证码 → 登录 today.ai → 提取 session cookie
      → 通过 Cloudflare API 写入 Worker secret TODAY_SESSION_COOKIE（可选再验证）

用法示例：
  # 交互式（发验证码后等你输入）
  python3 update_worker_cookie.py --email nian97865@gmail.com

  # 手动给验证码
  python3 update_worker_cookie.py --email nian97865@gmail.com --otp 123456

  # 自动从 Gmail/IMAP 抓验证码（需应用专用密码）
  python3 update_worker_cookie.py --email nian97865@gmail.com \
      --imap-host imap.gmail.com --imap-user nian97865@gmail.com --imap-pass xxxx

  # 只生成命令不执行（配合 wrangler 手动跑）
  python3 update_worker_cookie.py --email xxx --otp 123456 --dry-run

环境变量（均可被 --xxx 覆盖）:
  TODAYAI_EMAIL        账号邮箱
  CLOUDFLARE_API_TOKEN CF API Token（部署用的 cfut_...）
  CLOUDFLARE_ACCOUNT_ID CF Account ID
  GATEWAY_API_KEY      网关密钥（--test 时需要）
  TODAYAI_GATEWAY_URL  网关地址（--test 时需要）
  TODAYAI_IMAP_HOST / TODAYAI_IMAP_USER / TODAYAI_IMAP_PASS
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
BASE = "https://today.ai"
DEFAULT_WORKER = "todayai-proxy"
OTP_RE = re.compile(r"\b(\d{6})\b")


# ---------- HTTP ----------

def api(url: str, method: str = "GET", body=None, headers=None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return e.code, {"error": detail}


# ---------- 1) OTP 登录 ----------

def send_otp(email: str) -> None:
    code, data = api(f"{BASE}/api/auth/email-otp/send-verification-otp",
                     "POST", {"email": email, "type": "sign-in"})
    if code != 200 or not (data or {}).get("success"):
        raise RuntimeError(f"发送验证码失败: HTTP {code} {data}")
    print(f"📧 验证码已发送到 {email}，请查收")


def fetch_otp_imap(host: str, user: str, password: str, timeout_s: int = 120) -> str:
    """从 IMAP 收件箱自动抓取 Today 验证码（轮询直到超时）。"""
    import imaplib
    import email as emaillib
    from email.header import decode_header

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(host)
            M.login(user, password)
            M.select("INBOX")
            # 搜最近 10 封未读/全部，找 today.ai 验证码
            _, nums = M.search(None, "ALL")
            ids = (nums[0] or b"").split()[-10:]
            for num in reversed(ids):
                _, msg_data = M.fetch(num, "(RFC822)")
                msg = emaillib.message_from_bytes(msg_data[0][1])
                subject = str(decode_header(msg.get("Subject", ""))[0][0])
                sender = str(msg.get("From", ""))
                if "today" not in (subject + sender).lower():
                    continue
                # 收集正文文本
                parts = []
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            parts.append(part.get_payload(decode=True).decode("utf-8", "replace"))
                else:
                    parts.append(msg.get_payload(decode=True).decode("utf-8", "replace"))
                text = " ".join(parts)
                m = OTP_RE.search(text)
                if m:
                    M.logout()
                    return m.group(1)
            M.logout()
        except Exception as e:  # noqa: BLE001
            print(f"  ⏳ IMAP 读取中... ({e})", file=sys.stderr)
        time.sleep(5)
    raise RuntimeError(f"等待验证码超时（{timeout_s}s），请换 --otp 手动输入")


def sign_in(email: str, otp: str) -> dict:
    code, data = api(f"{BASE}/api/auth/sign-in/email-otp",
                     "POST", {"email": email, "otp": otp})
    if code != 200:
        raise RuntimeError(f"登录失败: HTTP {code} {data}")
    if not data.get("token"):
        raise RuntimeError(f"登录响应缺少 token: {data}")
    expires = time.strftime("%Y-%m-%d", time.localtime(time.time() + 59 * 86400))
    return {"name": (data.get("user") or {}).get("name", "?"),
            "token": data["token"],
            "expires_est": expires}


# ---------- 2) 写入 CF Worker secret ----------

def update_worker_secret(cf_token: str, account_id: str, worker: str, cookie: str, dry_run: bool = False) -> None:
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/workers/scripts/{worker}/secrets")
    payload = {"name": "TODAY_SESSION_COOKIE", "text": cookie, "type": "secret_text"}
    if dry_run:
        print(f"🔧 [dry-run] 将 PUT {url}")
        print(f"🔧 [dry-run] body: {{name: TODAY_SESSION_COOKIE, text: <cookie {len(cookie)} 字符>}}")
        return
    code, data = api(url, "PUT", payload, headers={"Authorization": f"Bearer {cf_token}"})
    if code not in (200, 201):
        raise RuntimeError(f"写入 Worker secret 失败: HTTP {code} {data}")
    print(f"✅ Worker secret 更新成功: {data.get('result', {}).get('name', 'TODAY_SESSION_COOKIE')}")

    # 部署使 secret 生效
    deploy_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker}/subdomain"
    # 注意: worker 内容未变时, secret 更新即可生效; 无需重新 deploy
    print("ℹ️ Secret 已生效（CF 的 secret 是运行时注入，无需重新部署）")


# ---------- 3) 验证 ----------

def verify_gateway(gateway_url: str, api_key: str) -> None:
    import urllib.parse
    url = f"{gateway_url}/chat/completions"
    body = {"model": "today-fast", "messages": [{"role": "user", "content": "ping，只回 pong"}], "stream": False}
    code, data = api(url, "POST", body, headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    if code != 200:
        raise RuntimeError(f"验证失败: HTTP {code} {data}")
    reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print(f"✅ 网关验证通过: {reply}")


# ---------- 主流程 ----------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="一键刷新 today.ai cookie 到 CF Worker", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--email", default=os.environ.get("TODAYAI_EMAIL"), help="today.ai 邮箱")
    p.add_argument("--otp", help="验证码（缺省交互输入；配 --imap-* 可自动抓）")
    p.add_argument("--imap-host", default=os.environ.get("TODAYAI_IMAP_HOST"), help="IMAP 服务器(自动抓码)")
    p.add_argument("--imap-user", default=os.environ.get("TODAYAI_IMAP_USER"), help="IMAP 用户(通常同邮箱)")
    p.add_argument("--imap-pass", default=os.environ.get("TODAYAI_IMAP_PASS"), help="IMAP 密码/应用专用密码")
    p.add_argument("--cf-token", default=os.environ.get("CLOUDFLARE_API_TOKEN"), help="CF API Token")
    p.add_argument("--cf-account", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"), help="CF Account ID")
    p.add_argument("--worker", default=DEFAULT_WORKER, help=f"Worker 名称（默认 {DEFAULT_WORKER}）")
    p.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不真正提交")
    p.add_argument("--verify", action="store_true", help="更新后调用网关验证")
    p.add_argument("--gateway-url", default=os.environ.get("TODAYAI_GATEWAY_URL", f"https://{DEFAULT_WORKER}.asd0611.workers.dev/v1"))
    p.add_argument("--api-key", default=os.environ.get("GATEWAY_API_KEY"), help="网关密钥(--verify 用)")
    args = p.parse_args(argv)

    if not args.email:
        print("❌ 需要 --email 或 TODAYAI_EMAIL", file=sys.stderr)
        return 2
    if not args.dry_run and not (args.cf_token and args.cf_account):
        print("❌ 需要 --cf-token/--cf-account 或对应环境变量（--dry-run 可跳过）", file=sys.stderr)
        return 2

    # 1) 发验证码
    print("📨 第 1 步：发送 OTP 验证码...")
    send_otp(args.email)

    # 2) 拿验证码
    otp = args.otp
    if not otp and args.imap_host:
        print(f"📬 第 2 步：自动从 IMAP({args.imap_host}) 抓验证码...")
        otp = fetch_otp_imap(args.imap_host, args.imap_user or args.email, args.imap_pass)
        print(f"🔑 抓到验证码: {otp}")
    elif not otp:
        otp = input("🔢 请输入邮箱收到的验证码: ").strip()
    if not re.fullmatch(r"\d{6}", otp or ""):
        print("❌ 验证码格式应为 6 位数字", file=sys.stderr)
        return 2

    # 3) 登录拿 cookie
    print("🔐 第 3 步：OTP 登录并提取 session cookie...")
    info = sign_in(args.email, otp)
    cookie = f"__Secure-better-auth.session_token={info['token']}"
    print(f"👤 登录用户: {info['name']} | session 预计有效期至 {info['expires_est']}")
    print(f"🍪 Cookie: {cookie[:40]}...（{len(cookie)} 字符，已脱敏）")

    # 4) 写入 CF Worker
    print(f"🛠 第 4 步：写入 Worker '{args.worker}' secret TODAY_SESSION_COOKIE...")
    update_worker_secret(args.cf_token, args.cf_account, args.worker, cookie, dry_run=args.dry_run)
    if args.dry_run:
        print("ℹ️ dry-run 完成，未实际修改")

    # 5) 验证
    if args.verify:
        if not args.api_key:
            print("⚠️ 跳过验证：缺 --api-key / GATEWAY_API_KEY", file=sys.stderr)
        else:
            print("🧪 第 5 步：调用网关验证...")
            verify_gateway(args.gateway_url, args.api_key)

    # 6) 提示保存 cookie 给 direct 模式
    print("💡 提示：direct 模式可把 cookie 存到环境变量 TODAYAI_SESSION_COOKIE（有效期约 59 天）")
    return 0


if __name__ == "__main__":
    sys.exit(main())