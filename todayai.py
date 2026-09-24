#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
todayai.py — Today AI (today.ai) Python 客户端 / 工作流适配器
============================================================
两种接入模式：
  1) direct   直连上游（本机持有 session cookie → 自动换 API token）
  2) gateway  走 CF Worker 网关（OpenAI 兼容端点，适合多客户端/无 cookie 场景）

既能当命令行用，也能当库 import，还支持 stdin 管道 / JSON 输出，
方便接入 n8n、Dify、自建 Pipeline 等工作流。

命令行示例：
  python3 todayai.py "你好"                          # 简单对话(自动检测模式)
  python3 todayai.py "写首诗" --model today-power    # 指定档位
  python3 todayai.py "总结下" --stream               # 流式打印
  python3 todayai.py "查资料" --json                 # JSON 输出(工作流友好)
  echo "来自管道的消息" | python3 todayai.py         # stdin 模式
  python3 todayai.py --system "你是翻译官" "hello"   # 带系统提示

库用法：
  from todayai import TodayAIClient
  c = TodayAIClient.from_env()
  print(c.chat("你好"))

配置（环境变量）：
  TODAYAI_MODE          direct | gateway（默认自动: 有 GATEWAY_URL 走网关）
  TODAYAI_SESSION_COOKIE  direct 模式必需：__Secure-better-auth.session_token=...（可带 ; 多段）
  TODAYAI_BASE_URL      direct 模式上游（默认 https://today.ai）
  TODAYAI_GATEWAY_URL   gateway 模式网关地址（默认 https://todayai-proxy.asd0611.workers.dev/v1）
  TODAYAI_API_KEY       gateway 模式密钥（= GATEWAY_API_KEY）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar

# ---------- 模型映射 ----------
MODELS = {
    "today-fast": "mode.fast",
    "today-balanced": "mode.balanced",
    "today-power": "mode.power",
    "today": "mode.balanced",
}
DEFAULT_MODEL = "today-balanced"
DEFAULT_BASE_URL = "https://today.ai"
DEFAULT_GATEWAY_URL = "https://todayai-proxy.asd0611.workers.dev/v1"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


class TodayAIError(RuntimeError):
    """统一错误类型：message 可带 HTTP 状态码与来源"""


@dataclass
class TodayAIClient:
    """Today AI 客户端：direct(直连) 与 gateway(网关) 双模式。"""

    mode: str = "auto"
    session_cookie: Optional[str] = None
    base_url: str = DEFAULT_BASE_URL
    gateway_url: str = DEFAULT_GATEWAY_URL
    api_key: Optional[str] = None
    model: str = DEFAULT_MODEL
    timeout: float = 180.0
    poll_interval: float = 1.5
    max_wait: float = 120.0

    # direct 模式 token 内存缓存
    _token_cache: dict = field(default_factory=lambda: {"token": None, "expires_at": 0.0})

    # ---------- 构造 ----------

    @classmethod
    def from_env(cls, env=None) -> "TodayAIClient":
        env = env or os.environ
        mode = env.get("TODAYAI_MODE", "auto").strip().lower()
        base_url = env.get("TODAYAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        gateway_url = env.get("TODAYAI_GATEWAY_URL", DEFAULT_GATEWAY_URL).rstrip("/")
        return cls(
            mode=mode,
            session_cookie=env.get("TODAYAI_SESSION_COOKIE") or None,
            base_url=base_url,
            gateway_url=gateway_url,
            api_key=env.get("TODAYAI_API_KEY") or None,
            model=env.get("TODAYAI_MODEL", DEFAULT_MODEL),
        )

    @classmethod
    def for_gateway(cls, gateway_url: str, api_key: str, model: str = DEFAULT_MODEL) -> "TodayAIClient":
        return cls(mode="gateway", gateway_url=gateway_url.rstrip("/"), api_key=api_key, model=model)

    @classmethod
    def for_direct(cls, session_cookie: str, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL) -> "TodayAIClient":
        return cls(mode="direct", session_cookie=session_cookie, base_url=base_url.rstrip("/"), model=model)

    def _resolve_mode(self) -> str:
        if self.mode == "auto":
            return "gateway" if self.gateway_url and self.api_key else "direct"
        return self.mode

    # ---------- HTTP 基础 ----------

    def _request(self, url: str, method: str = "GET", body: Optional[dict] = None,
                 headers: Optional[dict] = None, raw: bool = False):
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    return payload.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise TodayAIError(f"HTTP {e.code} {url}\n{detail}") from e
        except urllib.error.URLError as e:
            raise TodayAIError(f"网络错误 {url}: {e.reason}") from e

    # ---------- direct 模式核心 ----------

    def _get_api_token(self) -> str:
        now = time.time()
        if self._token_cache["token"] and self._token_cache["expires_at"] > now + 60:
            return self._token_cache["token"]
        if not self.session_cookie:
            raise TodayAIError("direct 模式需要 TODAYAI_SESSION_COOKIE（或改用 gateway 模式）")

        data = self._request(
            f"{self.base_url}/api/token",
            method="POST",
            body={"audience": f"https://api.{self.base_url.split('//')[-1].split('/')[0]}", "expiresIn": 3600},
            headers={"Cookie": self.session_cookie},
        )
        if not data.get("token"):
            raise TodayAIError(f"换 API token 失败: {data}")
        self._token_cache = {"token": data["token"], "expires_at": now + int(data.get("expiresIn", 3600))}
        return data["token"]

    def _send_message(self, content: str, mode: str) -> str:
        token = self._get_api_token()
        api_url = self.base_url.replace("https://", "https://api.").replace("http://", "http://api.")
        # 常见情况: today.ai -> api.today.ai；若 base_url 本身就是 api. 开头则不重复
        if "api." in self.base_url and self.base_url.split("/")[2].startswith("api."):
            api_url = self.base_url
        body = {"content": content, "role": "user"}
        if mode and mode != "mode.balanced":
            body["mode"] = mode
        data = self._request(
            f"{api_url}/v1/messages",
            method="POST",
            body=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        thread_id = data.get("threadId") if isinstance(data, dict) else None
        if not thread_id:
            raise TodayAIError(f"发送消息失败: {data}")
        return thread_id

    def _wait_reply(self, thread_id: str, max_wait: Optional[float] = None) -> str:
        token = self._get_api_token()
        api_url = self.base_url.replace("https://", "https://api.").replace("http://", "http://api.")
        if "api." in self.base_url and self.base_url.split("/")[2].startswith("api."):
            api_url = self.base_url
        deadline = time.time() + (max_wait or self.max_wait)
        last_err: Optional[str] = None
        while time.time() < deadline:
            try:
                data = self._request(
                    f"{api_url}/v1/messages?threadId={urllib.parse.quote(thread_id)}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except TodayAIError as e:
                last_err = str(e)
                time.sleep(self.poll_interval)
                continue
            items = data.get("data", []) if isinstance(data, dict) else []
            for msg in reversed(items):
                if msg.get("role") == "assistant" and msg.get("isFinal") and msg.get("content"):
                    return msg["content"]
            last_err = None
            time.sleep(self.poll_interval)
        raise TodayAIError(last_err or "等待回复超时")

    # ---------- gateway 模式 ----------

    def _chat_gateway(self, messages: list, stream: bool) -> str:
        if not self.api_key:
            raise TodayAIError("gateway 模式需要 TODAYAI_API_KEY")
        url = f"{self.gateway_url}/chat/completions"
        body = {"model": self.model, "messages": messages, "stream": stream}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        if not stream:
            data = self._request(url, method="POST", body=body, headers=headers)
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise TodayAIError(f"网关响应异常: {data}")
        else:
            # 流式：逐块解析 SSE，同时打印，最后返回完整文本
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
            req.add_header("User-Agent", UA)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "text/event-stream")
            req.add_header("Authorization", f"Bearer {self.api_key}")

            collected: list[str] = []
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    buf = b""
                    while True:
                        chunk = resp.read(1024)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n\n" in buf:
                            raw_line, buf = buf.split(b"\n\n", 1)
                            line = raw_line.decode("utf-8", "replace").strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                obj = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            for ch in obj.get("choices", []):
                                delta = ch.get("delta", {})
                                tok = delta.get("content", "")
                                if tok:
                                    collected.append(tok)
                                    print(tok, end="", flush=True)
                        if buf.startswith(b"data: [DONE]"):
                            break
                print()
                return "".join(collected)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                raise TodayAIError(f"网关流式请求失败 HTTP {e.code}:\n{detail}") from e
            except urllib.error.URLError as e:
                raise TodayAIError(f"网络错误 {url}: {e.reason}") from e

    # ---------- 统一入口 ----------

    def chat(self, text: str, system: Optional[str] = None, stream: bool = False,
             model: Optional[str] = None, timeout: Optional[float] = None) -> str:
        """单轮对话。返回 assistant 文本；stream=True 时同时打印。"""
        if timeout:
            self.timeout = timeout
        if model:
            self.model = model
        mode = self._resolve_mode()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": text})

        if mode == "gateway":
            return self._chat_gateway(messages, stream=stream)
        # direct
        if stream:
            # 直连上游本身是"发消息→轮询"，没有真正流式；打印整段即可
            text_out = self._direct_chat(text, system, model)
            print(text_out)
            return text_out
        return self._direct_chat(text, system, model)

    def _direct_chat(self, text: str, system: Optional[str], model: Optional[str]) -> str:
        model = model or self.model
        m = MODELS.get(model)
        if not m:
            raise TodayAIError(f"未知模型 {model}（可选: {', '.join(MODELS)}）")
        prompt = text
        if system:
            prompt = f"[系统] {system}\n[用户] {text}"
        thread_id = self._send_message(prompt, m)
        return self._wait_reply(thread_id)

    def list_models(self) -> list:
        """返回支持的模型列表（gateway 模式会从远端拉取）。"""
        if self._resolve_mode() == "gateway":
            data = self._request(
                f"{self.gateway_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            return [m["id"] for m in data.get("data", [])]
        return list(MODELS)


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="todayai",
        description="Today AI 客户端 / 工作流适配器（direct 直连 + gateway 网关）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("text", nargs="?", help="用户消息（缺省时从 stdin 读取）")
    p.add_argument("--mode", choices=["auto", "direct", "gateway"], default=None, help="接入模式（默认 auto）")
    p.add_argument("--model", choices=list(MODELS), default=None, help="模型档位（默认 today-balanced）")
    p.add_argument("--system", help="系统提示词")
    p.add_argument("--stream", action="store_true", help="流式输出（仅 gateway 模式真正流式）")
    p.add_argument("--json", action="store_true", help="输出 JSON（工作流友好）")
    p.add_argument("--timeout", type=float, default=None, help="请求超时秒数（默认 180）")
    p.add_argument("--cookie", help="direct 模式 session cookie（覆盖环境变量）")
    p.add_argument("--gateway-url", help="gateway 模式网关地址（覆盖环境变量）")
    p.add_argument("--api-key", help="gateway 模式密钥（覆盖环境变量）")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)

    # stdin 模式
    text = args.text
    if text is None:
        if sys.stdin.isatty():
            build_parser().print_help()
            return 2
        text = sys.stdin.read().strip()
    if not text:
        print("错误: 消息不能为空", file=sys.stderr)
        return 2

    client = TodayAIClient.from_env()
    if args.mode:
        client.mode = args.mode
    if args.model:
        client.model = args.model
    if args.cookie:
        client.session_cookie = args.cookie
    if args.gateway_url:
        client.gateway_url = args.gateway_url.rstrip("/")
    if args.api_key:
        client.api_key = args.api_key
    if args.timeout:
        client.timeout = args.timeout

    start = time.time()
    try:
        reply = client.chat(text, system=args.system, stream=args.stream)
    except TodayAIError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 兜底
        if args.json:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        else:
            print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        out = {
            "ok": True,
            "model": client.model,
            "mode": client._resolve_mode(),
            "reply": reply,
            "elapsed_s": round(time.time() - start, 2),
        }
        print(json.dumps(out, ensure_ascii=False))
    elif not args.stream:  # stream 时已打印
        print(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())