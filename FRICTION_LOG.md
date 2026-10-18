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
