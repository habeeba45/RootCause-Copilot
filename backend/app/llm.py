"""LLM integration (optional) - Anthropic or any OpenAI-compatible API, with tool calling.

Configuration comes ONLY from environment variables (or backend/.env, which is git-ignored).
API keys are never logged, stored in incidents, or sent to the browser.

    ANTHROPIC_API_KEY=...        default model claude-sonnet-5-5
    OPENAI_API_KEY=...           default model gpt-4o-mini
    OPENAI_BASE_URL=...          optional: any OpenAI-compatible endpoint (Azure, OpenRouter, Ollama…)
    LLM_PROVIDER=anthropic|openai|none
    LLM_MODEL=...                override the model
    LLM_TIMEOUT=60               seconds per request
    LLM_MAX_STEPS=8              tool calls the LLM planner may make per investigation
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_ENV_LOADED = False
_TRANSPORT: httpx.BaseTransport | None = None  # tests inject an httpx.MockTransport here


class LLMError(RuntimeError):
    pass


def load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def status() -> dict:
    """Public, key-free description of the configured LLM."""
    load_env()
    forced = os.getenv("LLM_PROVIDER", "").lower()
    off = {"enabled": False, "provider": None, "model": None, "mode": "rule-based"}
    if forced == "none":
        return off
    if forced in ("", "anthropic") and os.getenv("ANTHROPIC_API_KEY"):
        return {"enabled": True, "provider": "anthropic", "model": os.getenv("LLM_MODEL", "claude-sonnet-5-5"),
                "mode": "llm-assisted"}
    if forced in ("", "openai") and os.getenv("OPENAI_API_KEY"):
        return {"enabled": True, "provider": "openai", "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
                "mode": "llm-assisted", "base_url_custom": bool(os.getenv("OPENAI_BASE_URL"))}
    return off


def max_steps() -> int:
    try:
        return max(1, min(20, int(os.getenv("LLM_MAX_STEPS", "8"))))
    except ValueError:
        return 8


def _timeout() -> float:
    try:
        return float(os.getenv("LLM_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def _redact(text: str) -> str:
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        v = os.getenv(k)
        if v:
            text = text.replace(v, "***")
    return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", text)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str | None = None
    usage: dict = field(default_factory=dict)


class LLMSession:
    """A provider-agnostic conversation that supports tool calling.

    tools: [{"name", "description", "parameters": <JSON schema>}]
    """

    def __init__(self, system: str, tools: list[dict]):
        st = status()
        if not st["enabled"]:
            raise LLMError("No LLM configured")
        self.provider = st["provider"]
        self.model = st["model"]
        self.system = system
        self.tools = tools
        self.messages: list[dict] = []
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    # ------------------------------------------------------------------ public
    def send(self, user_text: str | None = None, tool_results: list[tuple[str, dict]] | None = None,
             force_tool: str | None = None) -> LLMReply:
        if self.provider == "anthropic":
            content: list[dict] = []
            for tid, result in tool_results or []:
                block = {"type": "tool_result", "tool_use_id": tid, "content": json.dumps(result, default=str)}
                if isinstance(result, dict) and result.get("ok") is False:
                    block["is_error"] = True  # tells the model the call was rejected (bad service name, etc.)
                content.append(block)
            if user_text:
                content.append({"type": "text", "text": user_text})
            self.messages.append({"role": "user", "content": content})
            return self._anthropic(force_tool)
        for tid, result in tool_results or []:
            self.messages.append({"role": "tool", "tool_call_id": tid, "content": json.dumps(result, default=str)})
        if user_text:
            self.messages.append({"role": "user", "content": user_text})
        return self._openai(force_tool)

    # ------------------------------------------------------------------ providers
    def _post(self, url: str, headers: dict, body: dict) -> dict:
        self.calls += 1
        try:
            with httpx.Client(timeout=_timeout(), transport=_TRANSPORT) as client:
                r = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise LLMError(_redact(f"{type(e).__name__}: {e}")) from None
        if r.status_code >= 400:
            raise LLMError(_redact(f"HTTP {r.status_code} from {self.provider}: {r.text[:300]}"))
        try:
            return r.json()
        except ValueError:
            raise LLMError("LLM returned a non-JSON response") from None

    def _anthropic(self, force_tool: str | None) -> LLMReply:
        body = {"model": self.model, "max_tokens": 2048, "system": self.system, "messages": self.messages,
                "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                          for t in self.tools]}
        if force_tool:
            body["tool_choice"] = {"type": "tool", "name": force_tool}
        data = self._post("https://api.anthropic.com/v1/messages",
                          {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                           "content-type": "application/json"}, body)
        blocks = data.get("content") or []
        self.messages.append({"role": "assistant", "content": blocks})
        u = data.get("usage") or {}
        self.usage["input_tokens"] += u.get("input_tokens", 0)
        self.usage["output_tokens"] += u.get("output_tokens", 0)
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [ToolCall(b.get("id", ""), b.get("name", ""), b.get("input") or {})
                 for b in blocks if b.get("type") == "tool_use"]
        return LLMReply(text.strip(), calls, data.get("stop_reason"))

    def _openai(self, force_tool: str | None) -> LLMReply:
        base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        body = {"model": self.model, "temperature": 0.1,
                "messages": [{"role": "system", "content": self.system}] + self.messages,
                "tools": [{"type": "function", "function": t} for t in self.tools]}
        if force_tool:
            body["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
        data = self._post(f"{base}/chat/completions", {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, body)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("Unexpected response shape from OpenAI-compatible API") from None
        self.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        u = data.get("usage") or {}
        self.usage["input_tokens"] += u.get("prompt_tokens", 0)
        self.usage["output_tokens"] += u.get("completion_tokens", 0)
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": fn.get("arguments")}
            calls.append(ToolCall(tc.get("id", ""), fn.get("name", ""), args if isinstance(args, dict) else {}))
        return LLMReply((msg.get("content") or "").strip(), calls, choice.get("finish_reason"))


def test_connection() -> dict:
    """Make one tiny request to prove the key, model and network work. Never returns the key."""
    st = status()
    if not st["enabled"]:
        return {"ok": False, "error": "No API key configured. Add ANTHROPIC_API_KEY or OPENAI_API_KEY to backend/.env "
                                     "and restart the backend.", **st}
    t0 = time.time()
    try:
        s = LLMSession("Reply with the single word: ready", [{"name": "noop", "description": "unused",
                                                               "parameters": {"type": "object", "properties": {}}}])
        reply = s.send("ping")
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000), "reply": reply.text[:40], **st}
    except LLMError as e:
        return {"ok": False, "error": str(e), **st}
