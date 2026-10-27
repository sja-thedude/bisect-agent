"use client";
import { useMemo, useState } from "react";
import { CallRecord, RunEvent, RunState, repoName, short, Step, tierLabel, usd } from "@/lib/api";
import { useRun } from "@/lib/useRun";
import { CodeView, DiffView } from "./DiffView";
import { Timeline } from "./Timeline";

const PHASES = ["clone", "reproduce", "confirm", "bisect", "research", "explain", "fix", "verify", "done"];

export function TierChip({ tier, model }: { tier: string; model?: string }) {
  const reason = tier === "reason";
  return (
    <span title={model} className={`inline-flex items-center gap-1 rounded px-1.5 py-[1px] font-mono text-[10.5px] font-medium uppercase tracking-wide ${reason ? "bg-nv/15 text-nv ring-1 ring-nv/30" : "bg-fast/10 text-fast ring-1 ring-fast/25"}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${reason ? "bg-nv" : "bg-fast"}`} />
      {tierLabel(tier)}
    </span>
  );
}

function VerdictChip({ v }: { v: string }) {
  const c = v === "good" ? "text-good bg-good/10 ring-good/30" : v === "bad" ? "text-bad bg-bad/10 ring-bad/30" : "text-skip bg-skip/10 ring-skip/30";
  return <span className={`rounded px-1.5 py-[1px] font-mono text-[11px] font-semibold uppercase ring-1 ${c}`}>{v}</span>;
}

function Panel({ title, right, children, className = "" }: { title: React.ReactNode; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-line bg-panel ${className}`}>
      <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
        <h3 className="text-[13px] font-semibold tracking-wide text-ink">{title}</h3>
        {right}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}

function PhaseBar({ phase, outcome, skipped }: { phase: string; outcome: string; skipped: Set<string> }) {
  const idx = PHASES.indexOf(phase);
  return (
    <ol className="flex flex-wrap gap-1.5">
      {PHASES.filter((p) => p !== "done").map((p, i) => {
        const done = idx > i || phase === "done";
        const active = idx === i && phase !== "done";
        const skip = skipped.has(p);
        return (
          <li key={p} className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11.5px] font-medium ring-1 ${
            active ? "bg-nv/15 text-nv ring-nv/40" : skip ? "text-muted/50 ring-line line-through" : done ? "text-ink ring-line bg-panel2" : "text-muted ring-line"}`}>
            {active && <span className="pulse-dot h-1.5 w-1.5 rounded-full bg-nv" />}
            {done && !skip && !active && <span className="text-good">✓</span>}
            {p}
          </li>
        );
      })}
      {phase === "done" && (
        <li className={`rounded-full px-2.5 py-1 text-[11.5px] font-semibold ring-1 ${outcome === "found" ? "text-good ring-good/40 bg-good/10" : outcome === "ambiguous" ? "text-skip ring-skip/40 bg-skip/10" : "text-bad ring-bad/40 bg-bad/10"}`}>
          {outcome || "done"}
        </li>
      )}
    </ol>
  );
}

function fmtTime(ts: number, t0: number) {
  const s = Math.max(0, ts - t0);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

function LogLine({ ev, t0 }: { ev: RunEvent; t0: number }) {
  const p = ev.payload;
  const time = <span className="mr-3 select-none text-muted/60">{fmtTime(ev.ts, t0)}</span>;
  switch (ev.kind) {
    case "phase":
      return <div className="mt-2 text-nv">{time}── {p.phase} ──</div>;
    case "log":
      return <div className={p.level === "error" ? "text-bad" : p.level === "warn" ? "text-skip" : "text-ink/90"}>{time}{p.message}</div>;
    case "call":
      return (
        <div className="text-muted">
          {time}<TierChip tier={p.tier} model={p.model} /> <span className="text-ink/80">{p.task}</span>
          {p.context && <span> @{short(p.context)}</span>} · {p.latency_s.toFixed(1)}s · {p.prompt_tokens.toLocaleString()}→{p.completion_tokens.toLocaleString()} tok
          {!p.ok && <span className="text-bad"> · {p.error}</span>}
        </div>
      );
    case "step_start":
      return <div className="text-ink/80">{time}testing <a className="text-fast hover:underline" href={p.url} target="_blank" rel="noreferrer">{short(p.sha)}</a> {p.subject} <span className="text-muted">({p.remaining} commits in range)</span></div>;
    case "step":
      return <div>{time}<VerdictChip v={p.verdict} /> <span className="text-ink/80">{short(p.sha)}</span> <span className="text-muted">pytest {p.status} in {p.duration_s.toFixed(1)}s — {p.reason}</span></div>;
    case "search":
      return <div className="text-ink/80">{time}<span className="rounded bg-[#2a1f4a] px-1.5 py-[1px] text-[10.5px] font-semibold uppercase text-[#c4b5fd] ring-1 ring-[#7c3aed]/40">Tavily</span> {p.purpose}: “{p.query}” → {p.hits.length} results</div>;
    case "first_bad":
      return <div className="font-semibold text-bad">{time}first bad commit: {short(p.sha)} {p.subject}</div>;
    case "repro":
      return <div className="text-ink/80">{time}reproduction test drafted (attempt {p.attempt}): <span className="text-muted">{p.symptom}</span></div>;
    case "research_decision":
      return <div className="text-[#c4b5fd]">{time}upstream research {p.needs_upstream_research ? "WARRANTED" : "not needed"}: <span className="text-muted">{p.reason}</span></div>;
    case "range":
      return <div className="text-ink/80">{time}bisect range: {p.commits.length} candidate commits{p.filtered_out > 0 && <span className="text-muted"> ({p.filtered_out} docs/CI-only commits filtered out)</span>}</div>;
    case "counterfactual":
      return <div className={p.status === "confirmed" ? "font-semibold text-good" : "text-skip"}>{time}causal check: {p.status === "confirmed" ? "reverting the culprit on HEAD makes the test PASS ✓" : p.status.replace("_", " ")}</div>;
    case "verification":
      return <div className={p.repro_passes && !p.new_failures.length ? "text-good" : "text-skip"}>{time}{p.verdict}</div>;
    default:
      return null;
  }
}

function StepsTable({ steps, pending }: { steps: Step[]; pending: { index: number; sha: string; subject: string; url: string } | null }) {
  return (
    <div className="overflow-x-auto scroll-thin">
      <table className="w-full text-[12.5px]">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wider text-muted">
            <th className="pb-2 pr-3 font-medium">#</th><th className="pb-2 pr-3 font-medium">commit</th>
            <th className="pb-2 pr-3 font-medium">verdict</th><th className="pb-2 pr-3 font-medium">classified by</th>
            <th className="pb-2 pr-3 text-right font-medium">test time</th>
          </tr>
        </thead>
        <tbody className="align-top">
          {steps.map((s) => (
            <tr key={s.index} className="border-t border-line/70">
              <td className="py-2 pr-3 font-mono text-muted">{s.index}</td>
              <td className="py-2 pr-3">
                <a href={s.url} target="_blank" rel="noreferrer" className="font-mono text-fast hover:underline">{short(s.sha)}</a>{" "}
                <span className="text-ink/90">{s.subject}</span>
                {s.summary && <div className="mt-0.5 text-[11.5px] text-muted">{s.summary}</div>}
                {s.reason && <div className="mt-0.5 text-[11.5px] text-muted/80 italic">{s.reason}</div>}
              </td>
              <td className="py-2 pr-3"><VerdictChip v={s.verdict} /><div className="mt-1 font-mono text-[10.5px] text-muted">pytest: {s.status}</div></td>
              <td className="py-2 pr-3">{s.tier ? <TierChip tier={s.tier} model={s.model} /> : <span className="text-muted">exit code</span>}</td>
              <td className="py-2 pr-3 text-right font-mono text-muted">
                {s.duration_s.toFixed(1)}s
                {s.speculative_hit && <div title="Tested speculatively in parallel before git asked for it" className="text-[10.5px] text-nv">prefetched</div>}
              </td>
            </tr>
          ))}
          {pending && (
            <tr className="border-t border-line/70">
              <td className="py-2 pr-3 font-mono text-muted">{pending.index}</td>
              <td className="py-2 pr-3"><span className="font-mono text-fast">{short(pending.sha)}</span> <span className="text-ink/90">{pending.subject}</span></td>
              <td className="py-2 pr-3" colSpan={3}><span className="inline-flex items-center gap-2 text-muted"><span className="pulse-dot h-2 w-2 rounded-full bg-nv" />running in sandbox…</span></td>
            </tr>
          )}
        </tbody>
      </table>
      {!steps.length && !pending && <p className="text-[12.5px] text-muted">Bisection starts once the reproduction test is confirmed.</p>}
    </div>
  );
}

function ModelPanel({ calls, economics }: { calls: CallRecord[]; economics: RunState["economics"] }) {
  const totals = useMemo(() => {
    const t: Record<string, { model: string; calls: number; pin: number; pout: number; lat: number; cost: number }> = {};
    for (const c of calls) {
      const x = (t[c.tier] ??= { model: c.model, calls: 0, pin: 0, pout: 0, lat: 0, cost: 0 });
      x.calls++; x.pin += c.prompt_tokens; x.pout += c.completion_tokens; x.lat += c.latency_s; x.cost += c.cost_usd ?? 0;
    }
    return t;
  }, [calls]);
  const actual = Object.values(totals).reduce((a, t) => a + t.cost, 0);
  return (
    <div className="space-y-3">
      {calls.length > 0 && (
        <div className="rounded-lg border border-nv/30 bg-nv/5 p-3">
          <div className="flex items-baseline justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">Run cost</span>
            <span className="font-mono text-lg font-semibold text-ink">{usd(actual)}</span>
          </div>
          {economics?.all_reason_usd ? (
            <p className="mt-1 text-[12px] leading-snug text-muted">
              Same tokens all on Ultra: <span className="font-mono text-ink">{usd(economics.all_reason_usd)}</span>.
              Routing saved <span className="font-semibold text-nv">{economics.saved_pct}%</span> while Nano handled{" "}
              <span className="text-ink">{Math.round((economics.fast_share_of_calls ?? 0) * 100)}%</span> of calls.
            </p>
          ) : null}
        </div>
      )}
      {(["reason", "fast"] as const).map((tier) => {
        const t = totals[tier];
        return (
          <div key={tier} className="rounded-lg border border-line bg-panel2 p-3">
            <div className="flex items-center justify-between"><TierChip tier={tier} /><span className="font-mono text-[11px] text-muted">{t?.model ?? "—"}</span></div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-center">
              <div><div className="font-mono text-lg text-ink">{t?.calls ?? 0}</div><div className="text-[10.5px] uppercase text-muted">calls</div></div>
              <div><div className="font-mono text-lg text-ink">{t ? ((t.pin + t.pout) / 1000).toFixed(1) + "k" : "0"}</div><div className="text-[10.5px] uppercase text-muted">tokens</div></div>
              <div><div className="font-mono text-lg text-ink">{t && t.calls ? (t.lat / t.calls).toFixed(1) + "s" : "—"}</div><div className="text-[10.5px] uppercase text-muted">avg latency</div></div>
            </div>
            {t && <div className="mt-1.5 text-right font-mono text-[11px] text-muted">{usd(t.cost)}</div>}
          </div>
        );
      })}
      <div className="max-h-72 overflow-auto scroll-thin">
        <table className="w-full text-[11.5px]">
          <tbody>
            {calls.map((c, i) => (
              <tr key={i} className="border-t border-line/60">
                <td className="py-1 pr-2"><TierChip tier={c.tier} model={c.model} /></td>
                <td className="py-1 pr-2 text-ink/85">{c.task}{c.context && <span className="text-muted"> @{short(c.context)}</span>}</td>
                <td className="py-1 pr-2 text-right font-mono text-muted">{c.latency_s.toFixed(1)}s</td>
                <td className="py-1 text-right font-mono text-muted">{(c.prompt_tokens + c.completion_tokens).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CausalProof({ st }: { st: RunState }) {
  const c = st.counterfactual;
  if (!c?.status) return null;
  const sha = short(st.first_bad.sha);
  const ok = c.status === "confirmed";
  const text = ok
    ? <>Reverting <span className="font-mono">{sha}</span> alone on today&apos;s HEAD makes the reproduction test <b>pass</b>{c.partial ? <> (partial revert; left {c.skipped_files?.join(", ")} untouched)</> : null}. This commit is why the bug exists now, not just where it started.{c.method === "model_resolved" && <> <span className="text-muted">(git revert conflicted with later reformatting; the reasoning tier resolved the revert against HEAD, and the sandbox ran the test.)</span></>}</>
    : c.status === "not_confirmed"
      ? <>Reverting <span className="font-mono">{sha}</span> on HEAD does not make the test pass ({c.pytest}): later commits also contribute.</>
      : c.status === "conflict"
        ? <>Could not revert <span className="font-mono">{sha}</span> on HEAD: later commits rewrote the same lines.</>
        : <>Counterfactual check could not run ({c.detail || c.status}).</>;
  return (
    <div className={`mt-3 flex items-start gap-3 rounded-lg border px-3 py-2.5 ${ok ? "border-good/40 bg-good/5" : "border-skip/40 bg-skip/5"}`}>
      <span className={`mt-0.5 rounded px-1.5 py-[1px] text-[10.5px] font-bold uppercase tracking-wide ${ok ? "bg-good/15 text-good" : "bg-skip/15 text-skip"}`}>
        {ok ? "causal proof ✓" : "causal check"}
      </span>
      <p className="text-[13px] leading-relaxed text-ink/90">{text}</p>
    </div>
  );
}

function issueMarkdown(st: RunState): string {
  const fb = st.first_bad, ex = st.explanation, v = st.fix?.verification;
  const lines = [
    `## Regression: ${ex.headline ?? fb.subject}`,
    "",
    `**First bad commit:** [\`${short(fb.sha)}\` ${fb.subject}](${fb.url})${fb.ambiguous ? " _(most likely; bisect was ambiguous)_" : ""}`,
    `**Range bisected:** \`${short(st.good_sha)}..${short(st.bad_sha)}\` (${st.range_size} commits, ${st.steps.length} tested)`,
    st.counterfactual?.status === "confirmed" ? `**Causal check:** reverting \`${short(fb.sha)}\` on HEAD makes the reproduction pass.` : "",
    "",
    "### Report",
    `> ${st.input.bug_report.replace(/\n/g, "\n> ")}`,
    "",
    "### What changed",
    ex.what_changed ?? "",
    "",
    "### Causal chain",
    ...(ex.causal_chain ?? []).map((c, i) => `${i + 1}. ${c}`),
    "",
    "### Reproduction",
    "```python",
    st.repro.code ?? "",
    "```",
  ];
  if (st.research?.findings?.length) {
    lines.push("", "### Upstream findings", ...st.research.findings.map((f) => `- ${f.fact} ([source](${f.url}))`));
  }
  if (st.fix?.diff) {
    lines.push("", `### Proposed fix${st.fix.verdict ? ` (${st.fix.verdict})` : ""}`, st.fix.rationale ?? "", "", "```diff", st.fix.diff.trimEnd(), "```");
    if (v) lines.push("", `Reproduction ${v.repro_passes ? "passes" : "still fails"}; full suite ${v.patched.total - v.patched.failing}/${v.patched.total} passing, ${v.new_failures.length} new failures.`);
  }
  lines.push("", `_Found by [Bisect](https://github.com/sja-thedude/bisect-agent) with NVIDIA Nemotron on Nebius Token Factory._`);
  return lines.filter((l, i, a) => !(l === "" && a[i - 1] === "")).join("\n");
}

function ExportButton({ st }: { st: RunState }) {
  const [done, setDone] = useState(false);
  return (
    <button onClick={() => { navigator.clipboard.writeText(issueMarkdown(st)); setDone(true); setTimeout(() => setDone(false), 1800); }}
      className="rounded-lg bg-ink px-3 py-1.5 text-[12.5px] font-semibold text-bg hover:bg-white">
      {done ? "Copied ✓" : "Copy as GitHub issue"}
    </button>
  );
}

function ReportCard({ st }: { st: RunState }) {
  const fb = st.first_bad;
  const ex = st.explanation;
  const v = st.fix?.verification;
  const research = st.research;
  return (
    <section className="overflow-hidden rounded-xl border border-line bg-panel">
      <div className={`border-b border-line px-5 py-4 ${st.outcome === "found" ? "bg-gradient-to-r from-bad/10 to-transparent" : "bg-gradient-to-r from-skip/10 to-transparent"}`}>
        <div className="text-[11px] font-semibold uppercase tracking-widest text-muted">
          {fb.sha ? (fb.ambiguous ? "Most likely first bad commit (ambiguous)" : "First bad commit") : st.outcome === "partial" ? "Partial findings" : "No culprit identified"}
        </div>
        {fb.sha ? (
          <div className="mt-1 flex flex-wrap items-baseline gap-x-3">
            <a href={fb.url} target="_blank" rel="noreferrer" className="font-mono text-xl font-semibold text-bad hover:underline">{short(fb.sha)}</a>
            <span className="text-lg font-medium text-ink">{fb.subject}</span>
            <span className="text-[12.5px] text-muted">{fb.author} · {fb.date?.slice(0, 10)}</span>
          </div>
        ) : (
          <p className="mt-1 text-ink/90">{st.error || st.limits_hit.join("; ") || "The run ended without isolating a commit."}</p>
        )}
        {ex.headline && <p className="mt-3 text-[15px] leading-relaxed text-ink">{ex.headline}</p>}
        {fb.sha && <CausalProof st={st} />}
        {fb.sha && (
          <div className="mt-3 flex flex-wrap gap-2">
            <ExportButton st={st} />
            {st.repro.code && (
              <a download="test_bisect_repro.py" href={`data:text/x-python;charset=utf-8,${encodeURIComponent(st.repro.code)}`}
                className="rounded-lg border border-line px-3 py-1.5 text-[12.5px] text-ink hover:border-nv/60">Download repro test</a>
            )}
          </div>
        )}
      </div>

      <div className="grid gap-6 p-5 lg:grid-cols-5">
        <div className="space-y-5 lg:col-span-3">
          {ex.what_changed && (
            <div>
              <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-widest text-muted">What changed</h4>
              <p className="text-[14px] leading-relaxed text-ink/90">{ex.what_changed}</p>
            </div>
          )}
          {!!ex.causal_chain?.length && (
            <div>
              <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-widest text-muted">Causal chain</h4>
              <ol className="relative space-y-2.5 border-l border-line pl-5">
                {ex.causal_chain.map((s, i) => (
                  <li key={i} className="text-[13.5px] leading-relaxed text-ink/90">
                    <span className={`absolute -left-[7px] mt-1.5 h-3 w-3 rounded-full border-2 border-panel ${i === ex.causal_chain!.length - 1 ? "bg-bad" : "bg-nv"}`} />
                    {s}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {ex.why_tests_missed_it && (
            <p className="text-[13px] text-muted"><span className="font-semibold text-ink/80">Why the suite missed it: </span>{ex.why_tests_missed_it}</p>
          )}
          {ex.confidence && <p className="text-[12px] text-muted">Confidence: <span className="font-semibold text-ink/80">{ex.confidence}</span> · explained by <TierChip tier="reason" /></p>}
        </div>

        <div className="space-y-4 lg:col-span-2">
          {research?.decision && (fb.dependency_files?.length ?? 0) > 0 && (
            <div className="rounded-lg border border-[#7c3aed]/40 bg-[#7c3aed]/5 p-3">
              <div className="mb-1 flex items-center gap-2">
                <span className="rounded bg-[#2a1f4a] px-1.5 py-[1px] text-[10.5px] font-semibold uppercase text-[#c4b5fd]">Upstream research · Tavily</span>
              </div>
              <p className="text-[12.5px] text-ink/85">{research.decision.reason}</p>
              {research.skipped && <p className="mt-1 text-[12px] text-skip">Skipped: {research.skipped}</p>}
              {!!fb.dependency_changes?.length && (
                <ul className="mt-2 space-y-0.5 font-mono text-[11.5px]">
                  {fb.dependency_changes.map((d) => <li key={d.package}><span className="text-ink">{d.package}</span> <span className="text-bad">{d.old ?? "∅"}</span> → <span className="text-good">{d.new ?? "∅"}</span></li>)}
                </ul>
              )}
              {research.upstream_change && <p className="mt-2 text-[12.5px] font-medium text-ink">{research.upstream_change}</p>}
              {!!research.findings?.length && (
                <ul className="mt-2 space-y-1.5">
                  {research.findings.map((f, i) => (
                    <li key={i} className="text-[12px] leading-snug text-ink/80">{f.fact} <a href={f.url} target="_blank" rel="noreferrer" className="break-all text-[#c4b5fd] hover:underline">[source]</a></li>
                  ))}
                </ul>
              )}
              {!!research.searches?.length && <p className="mt-2 text-[11px] text-muted">{research.searches.length} searches across changelogs, issue trackers and CVE databases.</p>}
            </div>
          )}
          {!!fb.files?.length && (
            <div>
              <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-widest text-muted">Files in the culprit</h4>
              <ul className="space-y-0.5 font-mono text-[12px] text-ink/80">{fb.files.slice(0, 12).map((f) => <li key={f}>{f}</li>)}</ul>
            </div>
          )}
          {!!ex.citations?.length && (
            <div>
              <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-widest text-muted">Citations</h4>
              <ul className="space-y-0.5 text-[12px]">{ex.citations.map((c) => <li key={c}><a href={c} target="_blank" rel="noreferrer" className="break-all text-fast hover:underline">{c}</a></li>)}</ul>
            </div>
          )}
          {!!st.candidates.length && !fb.sha && (
            <div>
              <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-widest text-muted">Narrowed to {st.candidates.length} commits</h4>
              <ul className="max-h-48 space-y-0.5 overflow-auto scroll-thin text-[12px]">{st.candidates.map((c) => <li key={c.sha}><a href={c.url} target="_blank" rel="noreferrer" className="font-mono text-fast hover:underline">{short(c.sha)}</a> {c.subject}</li>)}</ul>
            </div>
          )}
        </div>
      </div>

      {st.fix?.diff && (
        <div className="border-t border-line p-5">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-[11px] font-semibold uppercase tracking-widest text-muted">Proposed fix <TierChip tier="reason" /></h4>
            {st.fix.verdict && (
              <span className={`rounded-full px-2.5 py-1 text-[12px] font-semibold ring-1 ${st.fix.verdict?.endsWith("verified") ? "bg-good/10 text-good ring-good/40" : "bg-skip/10 text-skip ring-skip/40"}`}>{st.fix.verdict}</span>
            )}
          </div>
          {st.fix.rationale && <p className="mb-3 text-[13.5px] text-ink/90">{st.fix.rationale}</p>}
          {!!st.fix.attempts?.length && (
            <ol className="mb-3 flex flex-wrap gap-2 text-[11.5px]">
              {st.fix.attempts.map((a) => (
                <li key={a.attempt} title={a.detail} className={`rounded-full px-2.5 py-0.5 ring-1 ${a.result === "reproduction passes" ? "text-good ring-good/40" : "text-skip ring-skip/40"}`}>
                  attempt {a.attempt}: {a.result}
                </li>
              ))}
            </ol>
          )}
          <DiffView diff={st.fix.diff} />
          {v && (
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <Stat label="Reproduction test" value={v.repro_passes ? "passes" : "still fails"} tone={v.repro_passes ? "good" : "bad"} />
              <Stat label="Full suite at HEAD" value={`${v.baseline.total - v.baseline.failing}/${v.baseline.total} pass`} tone="muted" />
              <Stat label="Full suite with fix" value={`${v.patched.total - v.patched.failing}/${v.patched.total} pass · ${v.new_failures.length} new failures`} tone={v.new_failures.length ? "bad" : "good"} />
            </div>
          )}
          {!!v?.new_failures.length && <ul className="mt-2 font-mono text-[11.5px] text-bad">{v.new_failures.map((f) => <li key={f}>{f}</li>)}</ul>}
          {st.fix.apply_error && !st.fix.applied && <p className="mt-2 text-[12px] text-skip">Patch did not apply: {st.fix.apply_error}</p>}
        </div>
      )}
    </section>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone: "good" | "bad" | "muted" }) {
  const c = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : "text-ink";
  return (
    <div className="rounded-lg border border-line bg-panel2 px-3 py-2">
      <div className="text-[10.5px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`font-mono text-[13px] font-medium ${c}`}>{value}</div>
    </div>
  );
}

export function RunView({ id, onBack }: { id: string; onBack: () => void }) {
  const [speed, setSpeed] = useState(0);
  const [nonce, setNonce] = useState(0);
  const { events, state: st, error, streaming } = useRun(id, speed, nonce);
  const [copied, setCopied] = useState(false);

  const t0 = events[0]?.ts ?? st?.created_at ?? 0;
  const calls: CallRecord[] = useMemo(() => events.filter((e) => e.kind === "call").map((e) => e.payload), [events]);
  const steps: Step[] = useMemo(() => events.filter((e) => e.kind === "step").map((e) => e.payload), [events]);
  const pending = useMemo(() => {
    const starts = events.filter((e) => e.kind === "step_start");
    const last = starts[starts.length - 1]?.payload;
    return last && !steps.some((s) => s.index === last.index) && st?.phase !== "done" ? last : null;
  }, [events, steps, st?.phase]);
  const phase = useMemo(() => [...events].reverse().find((e) => e.kind === "phase")?.payload.phase ?? st?.phase ?? "queued", [events, st?.phase]);
  const repro = useMemo(() => [...events].reverse().find((e) => e.kind === "repro")?.payload, [events]);
  const rangeCommits: string[] = useMemo(() => {
    const ev = events.find((e) => e.kind === "range");
    return ev ? ev.payload.commits : (st?.range_commits ?? []).map((c) => c.sha);
  }, [events, st?.range_commits]);
  const culprit = useMemo(() => events.find((e) => e.kind === "first_bad")?.payload.sha as string | undefined, [events]);
  const skipped = useMemo(() => {
    const seen = new Set(events.filter((e) => e.kind === "phase").map((e) => e.payload.phase as string));
    return new Set(phase === "done" ? PHASES.filter((p) => !seen.has(p)) : []);
  }, [events, phase]);

  if (error) return <div className="rounded-xl border border-bad/40 bg-bad/5 p-6 text-bad">Could not load run: {error} <button onClick={onBack} className="ml-3 underline">back</button></div>;
  if (!st) return <div className="p-6 text-muted">Loading run…</div>;
  const finished = phase === "done";

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <button onClick={onBack} className="mb-2 text-[12.5px] text-muted hover:text-ink">← all runs</button>
          <h2 className="truncate text-xl font-semibold text-ink">
            <a href={st.repo_url || st.input.repo_url} target="_blank" rel="noreferrer" className="hover:underline">{repoName(st.repo_url || st.input.repo_url)}</a>
            {st.seed && <span className="ml-2 rounded bg-nv/10 px-1.5 py-0.5 align-middle text-[10.5px] font-semibold uppercase text-nv ring-1 ring-nv/30">verified demo</span>}
          </h2>
          <p className="mt-1 max-w-3xl text-[13.5px] leading-relaxed text-ink/80">“{st.input.bug_report}”</p>
          {st.good_sha && (
            <p className="mt-1 font-mono text-[11.5px] text-muted">range {short(st.good_sha)}..{short(st.bad_sha)} · {st.range_size} commits · python {st.input.python}</p>
          )}
        </div>
        <div className="flex gap-2">
          {finished && (
            <button onClick={() => { setSpeed(speed ? 0 : 25); setNonce((n) => n + 1); }} className="rounded-lg border border-line px-3 py-1.5 text-[12.5px] text-ink hover:border-nv/60">
              {speed ? "Show final result" : "▶ Replay run"}
            </button>
          )}
          <button onClick={() => { navigator.clipboard.writeText(location.href); setCopied(true); setTimeout(() => setCopied(false), 1500); }} className="rounded-lg border border-line px-3 py-1.5 text-[12.5px] text-ink hover:border-nv/60">
            {copied ? "Copied" : "Copy permalink"}
          </button>
        </div>
      </div>

      <PhaseBar phase={phase} outcome={st.outcome} skipped={skipped} />

      {finished && speed === 0 && <ReportCard st={st} />}

      <div className="grid gap-5 lg:grid-cols-3">
        <div className="space-y-5 lg:col-span-2">
          <Panel title="Bisection" right={<span className="font-mono text-[11px] text-muted">{steps.length} / 15 steps</span>}>
            {rangeCommits.length > 0 && <div className="mb-4"><Timeline commits={rangeCommits} steps={steps} culprit={culprit} pendingSha={pending?.sha} /></div>}
            <StepsTable steps={steps} pending={pending} />
          </Panel>
          <Panel title="Live log" right={streaming ? <span className="flex items-center gap-1.5 text-[11px] text-nv"><span className="pulse-dot h-1.5 w-1.5 rounded-full bg-nv" />live</span> : null}>
            <div className="max-h-[440px] space-y-0.5 overflow-auto scroll-thin font-mono text-[12px] leading-relaxed">
              {events.map((e) => <LogLine key={e.seq} ev={e} t0={t0} />)}
            </div>
          </Panel>
        </div>
        <div className="space-y-5">
          <Panel title="Model routing · Nebius Token Factory"><ModelPanel calls={calls} economics={st.economics} /></Panel>
          {(repro || st.repro.code) && (
            <Panel title="Reproduction test" right={<span className="font-mono text-[11px] text-muted">{st.repro.path}</span>}>
              {(repro?.symptom || st.repro.symptom) && <p className="mb-2 text-[12.5px] text-ink/85">{repro?.symptom || st.repro.symptom}</p>}
              <CodeView code={repro?.code || st.repro.code || ""} />
              <p className="mt-2 text-[11.5px] text-muted">Written by <TierChip tier="reason" />{(st.repro.attempts ?? 1) > 1 && ` after ${st.repro.attempts} attempts`}. Fails at HEAD{st.head_run.status ? ` (${st.head_run.status})` : ""}, passes at known-good{st.good_run.status ? ` (${st.good_run.status})` : ""}.</p>
            </Panel>
          )}
        </div>
      </div>

      {finished && speed > 0 && <ReportCard st={st} />}
    </div>
  );
}
