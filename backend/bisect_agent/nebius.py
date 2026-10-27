"""Minimal client for Nebius Token Factory (OpenAI-compatible chat completions).

This is the ONLY module that talks to an inference provider. There is no fallback
provider anywhere in the codebase, by design (hackathon rule).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import httpx

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"


class InferenceError(RuntimeError):
    pass


class _Deadline(Exception):
    pass


STALL_S = 60  # seconds without a single streamed byte before a request is treated as stalled


@dataclass
class Completion:
    content: str
    reasoning: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    model: str
    reasoning_tokens: int = 0


_THINK = re.compile(r"<think>(.*?)</think>", re.S)


def _split_reasoning(msg: dict) -> tuple[str, str]:
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    # Some deployments leave reasoning inline as <think>...</think>.
    m = _THINK.search(content)
    if m:
        reasoning = reasoning or m.group(1)
        content = _THINK.sub("", content)
    elif "</think>" in content:
        head, content = content.split("</think>", 1)
        reasoning = reasoning or head.replace("<think>", "")
    return content.strip(), reasoning.strip()


class TokenFactory:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key or os.environ.get("NEBIUS_API_KEY", "")
        self.base_url = (base_url or os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=15, read=300, write=30, pool=30),
            transport=transport,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        json_mode: bool = False,
        thinking: bool | None = None,
        retries: int = 3,
        deadline_s: float = 240.0,
    ) -> Completion:
        """Streams the completion. The read timeout applies between chunks, so a long but
        progressing generation is never cut off, a stalled one is detected within STALL_S, and
        `deadline_s` bounds each attempt overall. Non-streamed JSON responses are accepted too."""
        if not self.api_key:
            raise InferenceError("NEBIUS_API_KEY is not set")
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                      "stream": True, "stream_options": {"include_usage": True}}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if thinking is not None:
            # Nemotron 3 toggles reasoning through the chat template, passed as a top-level vLLM param.
            body["chat_template_kwargs"] = {"enable_thinking": thinking, "force_nonempty_content": True}
        delay = 2.0
        last = ""
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                with self._client.stream("POST", f"{self.base_url}/chat/completions", json=body,
                                         headers={"Authorization": f"Bearer {self.api_key}"},
                                         timeout=httpx.Timeout(connect=15, read=STALL_S, write=30, pool=30)) as r:
                    if r.status_code == 200:
                        return self._read(r, model, t0, deadline_s)
                    r.read()
                    last = f"HTTP {r.status_code}: {r.text[:500]}"
                    if r.status_code == 429:
                        delay = float(r.headers.get("retry-after", delay))
                    elif r.status_code < 500:
                        raise InferenceError(last)
            except _Deadline:
                last = f"no complete response within {deadline_s:.0f}s"
            except httpx.ReadTimeout:
                last = f"stream stalled (no data for {STALL_S}s)"
            except httpx.HTTPError as e:
                last = f"transport error: {e}"
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise InferenceError(last)

    @staticmethod
    def _read(r: httpx.Response, model: str, t0: float, deadline_s: float) -> Completion:
        if "text/event-stream" not in r.headers.get("content-type", ""):
            data = json.loads(r.read())
            msg, usage, model = data["choices"][0]["message"], data.get("usage") or {}, data.get("model", model)
        else:
            content: list[str] = []
            reasoning: list[str] = []
            usage = {}
            for line in r.iter_lines():
                if time.monotonic() - t0 > deadline_s:
                    raise _Deadline()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                ev = json.loads(payload)
                usage = ev.get("usage") or usage
                model = ev.get("model", model)
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        content.append(d["content"])
                    if d.get("reasoning_content") or d.get("reasoning"):
                        reasoning.append(d.get("reasoning_content") or d.get("reasoning"))
            msg = {"content": "".join(content), "reasoning_content": "".join(reasoning)}
        text, thought = _split_reasoning(msg)
        return Completion(
            content=text, reasoning=thought,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_s=round(time.monotonic() - t0, 3), model=model,
            reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
        )

    def list_models(self) -> list[str]:
        r = self._client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
