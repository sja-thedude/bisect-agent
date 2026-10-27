"""Model router: the single place that decides which Nemotron model handles which task.

Two tiers:

* REASON -> Nemotron 3 Ultra. Few calls, high value: writing the reproduction test,
  reading the culprit diff, explaining the causal chain, deciding whether upstream
  research is needed, and writing the fix. Thinking is ON.
* FAST   -> Nemotron 3 Nano. Many calls, must be cheap and quick: classifying each
  bisect step's pytest output, summarising each tested commit's diff, filtering
  commits when skips leave git bisect with several candidates, and turning a
  dependency bump into search queries. Thinking is OFF.

Every call is recorded (task, tier, model, latency, tokens) and pushed to listeners
so the UI can show which model handled which step.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable

from .nebius import Completion, InferenceError, TokenFactory


class Tier(str, Enum):
    REASON = "reason"
    FAST = "fast"


# Model IDs exactly as listed in the Token Factory catalog (casing is inconsistent upstream).
MODELS: dict[Tier, str] = {
    Tier.REASON: os.environ.get("BISECT_MODEL_REASON", "nvidia/Nemotron-3-Ultra-550b-a55b"),
    Tier.FAST: os.environ.get("BISECT_MODEL_FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"),
}

# Token Factory list prices, USD per 1M tokens (input, output), from the public model catalog.
PRICES: dict[Tier, tuple[float, float]] = {
    Tier.REASON: (float(os.environ.get("BISECT_PRICE_REASON_IN", 1.0)), float(os.environ.get("BISECT_PRICE_REASON_OUT", 3.0))),
    Tier.FAST: (float(os.environ.get("BISECT_PRICE_FAST_IN", 0.06)), float(os.environ.get("BISECT_PRICE_FAST_OUT", 0.24))),
}


def cost_usd(tier: Tier, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = PRICES[tier]
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000


# Per-tier generation settings.
TIER_PARAMS: dict[Tier, dict[str, Any]] = {
    Tier.REASON: {"thinking": True, "max_tokens": 24000, "temperature": 0.2},
    Tier.FAST: {"thinking": False, "max_tokens": 1200, "temperature": 0.0},
}

# Every task the agent performs, and the tier it is routed to. Adding a task means adding a row here.
ROUTES: dict[str, Tier] = {
    "write_repro_test": Tier.REASON,
    "repair_repro_test": Tier.REASON,
    "explain_culprit": Tier.REASON,
    "decide_upstream_research": Tier.REASON,
    "propose_fix": Tier.REASON,
    "resolve_revert": Tier.REASON,
    "classify_verdict": Tier.FAST,
    "summarise_diff": Tier.FAST,
    "filter_candidates": Tier.FAST,
    "select_context_files": Tier.FAST,
    "plan_search_queries": Tier.FAST,
    "digest_search_results": Tier.FAST,
}


@dataclass
class CallRecord:
    task: str
    tier: str
    model: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    ok: bool
    started_at: float
    error: str = ""
    context: str = ""  # e.g. the commit sha this call was about
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    content: str
    data: Any
    record: CallRecord
    reasoning: str = ""


class Router:
    def __init__(self, client: TokenFactory | None = None):
        self.client = client or TokenFactory()
        self.calls: list[CallRecord] = []
        self._listeners: list[Callable[[CallRecord], None]] = []
        self._lock = threading.Lock()

    def on_call(self, fn: Callable[[CallRecord], None]) -> None:
        self._listeners.append(fn)

    @staticmethod
    def tier_for(task: str) -> Tier:
        if task not in ROUTES:
            raise KeyError(f"no route for task {task!r}; add it to router.ROUTES")
        return ROUTES[task]

    @staticmethod
    def model_for(task: str) -> str:
        return MODELS[Router.tier_for(task)]

    def call(self, task: str, system: str, user: str, *, json_out: bool = False, context: str = "",
             max_tokens: int | None = None) -> Result:
        tier = self.tier_for(task)
        model = MODELS[tier]
        params = dict(TIER_PARAMS[tier])
        if max_tokens:
            params["max_tokens"] = max_tokens
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        started = time.time()
        attempts = 2 if json_out else 1
        comp: Completion | None = None
        err = ""
        data: Any = None
        for i in range(attempts):
            try:
                comp = self.client.chat(model, messages, json_mode=json_out, **params)
            except InferenceError as e:
                err = str(e)
                break
            if not json_out:
                break
            try:
                data = parse_json(comp.content)
                err = ""
                break
            except ValueError as e:
                err = f"unparseable JSON: {e}"
                messages = messages + [
                    {"role": "assistant", "content": comp.content[:4000]},
                    {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."},
                ]
        rec = CallRecord(
            task=task, tier=tier.value, model=model,
            latency_s=round(time.time() - started, 3),
            prompt_tokens=comp.prompt_tokens if comp else 0,
            completion_tokens=comp.completion_tokens if comp else 0,
            ok=not err, started_at=started, error=err[:300], context=context,
        )
        rec.cost_usd = round(cost_usd(tier, rec.prompt_tokens, rec.completion_tokens), 6)
        with self._lock:
            self.calls.append(rec)
        for fn in self._listeners:
            fn(rec)
        if err:
            raise InferenceError(f"{task}: {err}")
        return Result(content=comp.content, data=data, record=rec, reasoning=comp.reasoning)

    def totals(self) -> dict:
        out: dict[str, dict] = {}
        for c in self.calls:
            t = out.setdefault(c.tier, {"model": c.model, "calls": 0, "prompt_tokens": 0,
                                        "completion_tokens": 0, "latency_s": 0.0, "cost_usd": 0.0})
            t["calls"] += 1
            t["cost_usd"] = round(t["cost_usd"] + c.cost_usd, 6)
            t["prompt_tokens"] += c.prompt_tokens
            t["completion_tokens"] += c.completion_tokens
            t["latency_s"] = round(t["latency_s"] + c.latency_s, 3)
        return out


def economics(calls: list[dict]) -> dict:
    """What the routing decision saved: actual spend vs. the same token volume billed at Ultra
    prices (a lower bound, since Ultra with thinking on would also emit more tokens)."""
    actual = sum(c.get("cost_usd", 0.0) for c in calls)
    all_reason = sum(cost_usd(Tier.REASON, c["prompt_tokens"], c["completion_tokens"]) for c in calls)
    fast = [c for c in calls if c["tier"] == Tier.FAST.value]
    return {
        "actual_usd": round(actual, 5),
        "all_reason_usd": round(all_reason, 5),
        "saved_pct": round(100 * (1 - actual / all_reason), 1) if all_reason else 0.0,
        "fast_share_of_calls": round(len(fast) / len(calls), 3) if calls else 0.0,
    }


def parse_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(str(e)) from e
    raise ValueError("no JSON object found")
