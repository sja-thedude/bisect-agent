"""Minimal client for Nebius Token Factory (OpenAI-compatible chat completions).

This is the ONLY module that talks to an inference provider. There is no fallback
provider anywhere in the codebase, by design (hackathon rule).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import httpx

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"


class InferenceError(RuntimeError):
    pass


@dataclass
class Completion:
    content: str
    reasoning: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    model: str


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
        retries: int = 4,
    ) -> Completion:
        if not self.api_key:
            raise InferenceError("NEBIUS_API_KEY is not set")
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
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
                r = self._client.post(f"{self.base_url}/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as e:
                last = f"transport error: {e}"
            else:
                if r.status_code == 200:
                    data = r.json()
                    msg = data["choices"][0]["message"]
                    content, reasoning = _split_reasoning(msg)
                    usage = data.get("usage") or {}
                    return Completion(
                        content=content, reasoning=reasoning,
                        prompt_tokens=int(usage.get("prompt_tokens", 0)),
                        completion_tokens=int(usage.get("completion_tokens", 0)),
                        latency_s=round(time.monotonic() - t0, 3), model=data.get("model", model),
                    )
                last = f"HTTP {r.status_code}: {r.text[:500]}"
                if r.status_code == 429:
                    delay = float(r.headers.get("retry-after", delay))
                elif r.status_code < 500:
                    raise InferenceError(last)
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise InferenceError(last)

    def list_models(self) -> list[str]:
        r = self._client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
