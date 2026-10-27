export const API = process.env.NEXT_PUBLIC_API_BASE ?? "";

export type Tier = "reason" | "fast";

export interface CallRecord {
  task: string; tier: Tier; model: string; latency_s: number;
  prompt_tokens: number; completion_tokens: number; ok: boolean; started_at: number; error: string; context: string;
  cost_usd?: number;
}

export interface Step {
  index: number; sha: string; subject: string; status: string; verdict: "good" | "bad" | "skip";
  reason: string; duration_s: number; tier: string; model: string; summary: string; speculative_hit: boolean; url: string;
}

export interface SearchRecord {
  purpose: string; query: string; latency_s: number; answer: string; error: string; package?: string;
  hits: { title: string; url: string; content: string; score: number }[];
}

export interface RunState {
  id: string; phase: string; outcome: string; error: string; repo_url: string;
  input: { repo_url: string; bug_report: string; good_ref: string | null; bad_ref: string | null; python: string };
  good_sha: string; bad_sha: string; range_size: number;
  repro: { path?: string; code?: string; rationale?: string; symptom?: string; attempts?: number; context_files?: string[] };
  head_run: { status?: string; duration_s?: number; output_tail?: string };
  good_run: { status?: string; duration_s?: number };
  steps: Step[];
  first_bad: { sha?: string; subject?: string; author?: string; date?: string; url?: string; files?: string[];
    dependency_files?: string[]; dependency_changes?: { package: string; old: string | null; new: string | null }[];
    ambiguous?: boolean; ranking_reason?: string };
  candidates: { sha: string; subject: string; url: string }[];
  research: { decision?: { needs_upstream_research?: boolean; reason?: string }; searches?: SearchRecord[];
    findings?: { fact: string; url: string }[]; upstream_change?: string; skipped?: string; packages?: unknown[] };
  explanation: { headline?: string; what_changed?: string; causal_chain?: string[]; why_tests_missed_it?: string;
    confidence?: string; citations?: string[] };
  fix: { diff?: string; rationale?: string; applied?: boolean; apply_error?: string; verdict?: string; attempt?: number;
    attempts?: { attempt: number; result: string; detail?: string }[];
    verification?: { repro_passes: boolean; new_failures: string[];
      baseline: { status: string; total: number; failing: number; duration_s: number };
      patched: { status: string; total: number; failing: number; duration_s: number } } };
  counterfactual: { status?: "confirmed" | "not_confirmed" | "conflict" | "install_failed" | "error"; pytest?: string;
    duration_s?: number; partial?: boolean; skipped_files?: string[]; detail?: string; method?: "git_revert" | "model_resolved" };
  economics: { actual_usd?: number; all_reason_usd?: number; saved_pct?: number; fast_share_of_calls?: number };
  range_commits: { sha: string }[];
  limits_hit: string[]; calls: CallRecord[];
  model_totals: Record<string, { model: string; calls: number; prompt_tokens: number; completion_tokens: number; latency_s: number }>;
  created_at: number; finished_at: number; seed?: boolean; title?: string;
}

export interface RunSummary {
  id: string; created_at: number; finished_at: number | null; repo_url: string; bug_report: string; phase: string;
  outcome: string; seed: number; title: string; first_bad_sha: string | null; first_bad_subject: string | null;
}

export interface Config {
  provider: string; base_url: string;
  tiers: Record<Tier, { model: string; thinking: boolean; max_tokens: number }>;
  routes: Record<string, Tier>; limits: Record<string, number>;
  nebius_configured: boolean; tavily_configured: boolean; pythons: string[];
}

export interface RunEvent { seq: number; ts: number; kind: string; payload: any }

export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(API + path, { cache: "no-store" });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}`);
  return r.json();
}

export async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(API + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? `HTTP ${r.status}`));
  return data;
}

export const short = (sha?: string | null) => (sha ? sha.slice(0, 10) : "");
export const repoName = (url: string) => url.replace(/^https?:\/\/[^/]+\//, "").replace(/\.git$/, "");
export const usd = (n?: number) => (n == null ? "—" : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(3)}`);

export const tierLabel = (t: string) => (t === "reason" ? "Ultra" : t === "fast" ? "Nano" : t);
