# Friction log

Written live while building Bisect, for the Nebius and NVIDIA feedback forms.
Newest entries at the bottom. Each entry: date, tool, what happened, what I expected.

## 2026-10-19 — Nebius Token Factory docs (while wiring up the client)

- **Two base URLs in circulation.** Older guides and blog posts use `api.studio.nebius.com`; current docs use
  `api.tokenfactory.nebius.com`. Both still answer, and nothing says whether the old host is deprecated. I went with
  the new one. A one-line "the old host is an alias until <date>" note would save a search.
- **Model ID casing is inconsistent and matters.** `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`,
  `nvidia/nemotron-3-super-120b-a12b`, `nvidia/Nemotron-3-Ultra-550b-a55b`, `nvidia/Nemotron-3_5-Lightning`.
  Four naming conventions for one model family, and the cookbook spells Nano in lowercase while the catalog doesn't.
  I put the IDs in exactly one place (`router.py`) and made them env-overridable because of this.
- **Structured output docs contradict each other.** The chat-completions reference prose says only `json_object`
  or `text` is supported; the OpenAPI schema and the JSON guide say `json_schema` works too. The guide's Python
  example puts the schema directly under `json_schema` (not the OpenAI wrapper shape) and the curl example has a key
  spelled `"response format"` with a space. None of the Nemotron catalog entries carry the "JSON mode" tag, so I
  can't tell whether it's supported for them. I use `json_object` plus a tolerant parser and a one-shot repair turn.
- **Reasoning toggle isn't documented on the Nebius side.** The only place I found how to turn Nemotron 3 thinking
  on/off was NVIDIA's Hugging Face model cards (`chat_template_kwargs: {"enable_thinking": false}`). It's not clear
  whether Token Factory's own `reasoning_effort` parameter maps onto it. Docs are also silent on whether reasoning comes
  back in `reasoning_content` or inline `<think>` tags, so the client handles both.
- **Example model IDs in the docs point at removed models** (e.g. Kimi-K2.5, Qwen3-32B, Llama-3.3-70B were all retired in
  2026 and still appear in snippets). Deprecated IDs fail outright rather than redirecting.
- **Error body shape** is `{"detail": "..."}` rather than OpenAI's `{"error": {...}}`, so OpenAI-style error handling
  silently loses the message.

## 2026-10-19 — git (not a sponsor tool, but cost me 20 minutes)

- `git bisect start -- <pathspec>` rejects a pathspec made only of `:(exclude)` entries ("No testable commit found")
  even though `git rev-list` accepts the same pathspec. Adding an explicit `:(top)` include fixes it.

## 2026-10-27/28 — first live runs on Token Factory (106 calls, 0 errors)

- **The good:** both exact catalog IDs worked first time, `GET /v1/models` listed every Nemotron variant (handy for a
  preflight check), and across 106 calls in two tiers there were zero errors, zero 429s and no retries needed.
  Nano with thinking off answered classification calls in ~1.0 s median (max 1.4 s), which is what makes a model
  in the bisect loop affordable.
- **`chat_template_kwargs` just works** at the top level of the JSON body (as the vLLM pass-through implies), but
  nothing in the Token Factory docs says so; I only knew to try it from NVIDIA's model cards.
- **Ultra's thinking tokens dominate cost and latency, and there is no server-side budget.** A repair call produced
  19,287 completion tokens and took 65.7 s; median `propose_fix` output was ~6k tokens. NVIDIA's `reasoning_budget` is a
  client-side recipe, and it's unclear whether Token Factory's `reasoning_effort` maps to Nemotron's thinking at all.
  A documented `max_thinking_tokens` (or confirmation that `reasoning_effort` controls it) would help a lot.
- **Reasoning is reported well:** `usage.completion_tokens_details.reasoning_tokens` is populated (e.g. 344 of 347
  completion tokens on a short Ultra call) and the thinking comes back in `message.reasoning_content`, not inline
  `<think>` tags. Neither is mentioned in the Token Factory docs, which left me writing defensive parsing for both
  formats; one sentence in the docs would have saved that.
- **JSON mode was reliable on both tiers:** with `response_format: json_object` every response parsed; my one-shot
  "repair the JSON" turn never fired. Worth stating in the docs, since the catalog doesn't tag Nemotron as JSON-capable.
- **Model behaviour worth knowing (not a platform bug):** when asked for search/replace edits, Ultra sometimes copies
  the snippet from an older diff in the prompt rather than the current file. I added a whitespace/comment-insensitive
  locator and feed the real lines back on a miss.

## 2026-10-28 — Tavily

- `include_domains` scoping worked well: restricting CVE queries to nvd.nist.gov / osv.dev / github.com and issue
  queries to github.com gave noticeably more relevant hits. Advanced-depth searches took ~5 s each.
- `include_answer` returned a useful one-paragraph synthesis, but there is no way to ask for citations inside it, so I
  digest the raw results with Nemotron Nano to keep a URL attached to every fact.
