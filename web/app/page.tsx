"use client";
import { useCallback, useEffect, useState } from "react";
import { Config, getJSON, LIVE_URL, postJSON, repoName, RunSummary, short, STATIC } from "@/lib/api";
import { RunView, TierChip } from "@/components/RunView";

function useRunParam(): [string | null, (id: string | null) => void] {
  const [id, setId] = useState<string | null>(null);
  useEffect(() => {
    const read = () => setId(new URLSearchParams(location.search).get("run"));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const go = useCallback((next: string | null) => {
    history.pushState(null, "", next ? `/?run=${next}` : "/");
    setId(next);
    window.scrollTo({ top: 0 });
  }, []);
  return [id, go];
}

function Header({ onHome }: { onHome: () => void }) {
  return (
    <header className="border-b border-line bg-bg/80 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
        <button onClick={onHome} className="flex items-center gap-2.5">
          <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden>
            <rect x="1" y="1" width="24" height="24" rx="6" fill="#11151a" stroke="#76b900" strokeWidth="1.5" />
            <path d="M7 19V7M13 19V11M19 19V15" stroke="#76b900" strokeWidth="2.2" strokeLinecap="round" />
            <circle cx="13" cy="8" r="1.6" fill="#f85149" />
          </svg>
          <span className="text-[17px] font-semibold tracking-tight text-ink">Bisect</span>
          <span className="hidden text-[12.5px] text-muted sm:inline">autonomous regression hunter</span>
        </button>
        <div className="flex items-center gap-2 text-[11.5px] text-muted">
          <span className="hidden sm:inline">powered by</span>
          <span className="rounded bg-nv/10 px-1.5 py-0.5 font-semibold text-nv ring-1 ring-nv/30">NVIDIA Nemotron</span>
          <span className="hidden sm:inline">on</span>
          <span className="rounded bg-panel2 px-1.5 py-0.5 font-semibold text-ink ring-1 ring-line">Nebius Token Factory</span>
        </div>
      </div>
    </header>
  );
}

const EXAMPLE = {
  repo_url: "https://github.com/andialbrecht/sqlparse",
  bug_report: "After upgrading sqlparse, sqlparse.split() no longer splits a script wrapped in BEGIN TRANSACTION; ... END TRANSACTION; into separate statements. It comes back as a single statement.",
  good_ref: "0.5.3",
  bad_ref: "acd8e5817b5be5f9b9a7b159c1bacc45179f4e44",
};

function RunForm({ config, onStarted }: { config: Config | null; onStarted: (id: string) => void }) {
  const [repo, setRepo] = useState("");
  const [bug, setBug] = useState("");
  const [good, setGood] = useState("");
  const [bad, setBad] = useState("");
  const [python, setPython] = useState("3.11");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const r = await postJSON<{ id: string }>("/api/runs", { repo_url: repo, bug_report: bug, good_ref: good || null, bad_ref: bad || null, python });
      onStarted(r.id);
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const input = "w-full rounded-lg border border-line bg-bg px-3 py-2 text-[14px] text-ink placeholder:text-muted/60 outline-none focus:border-nv/60 focus:ring-2 focus:ring-nv/15";

  return (
    <form onSubmit={submit} className="space-y-3 rounded-xl border border-line bg-panel p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-[15px] font-semibold text-ink">Hunt a regression</h2>
        <button type="button" onClick={() => { setRepo(EXAMPLE.repo_url); setBug(EXAMPLE.bug_report); setGood(EXAMPLE.good_ref); setBad(EXAMPLE.bad_ref); }} className="text-[12px] text-muted hover:text-nv">fill example</button>
      </div>
      <label className="block">
        <span className="mb-1 block text-[12px] font-medium text-muted">Public Python repository (pytest)</span>
        <input className={input} required placeholder="https://github.com/owner/repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
      </label>
      <label className="block">
        <span className="mb-1 block text-[12px] font-medium text-muted">Bug report, in plain English</span>
        <textarea className={`${input} min-h-[110px] resize-y`} required minLength={20} placeholder="The CSV parser started choking on quoted newlines sometime after v2.1…" value={bug} onChange={(e) => setBug(e.target.value)} />
      </label>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="block">
          <span className="mb-1 block text-[12px] font-medium text-muted">Known-good ref <span className="text-muted/60">(optional)</span></span>
          <input className={input} placeholder="v2.1 · default: 200 commits back" value={good} onChange={(e) => setGood(e.target.value)} />
        </label>
        <label className="block">
          <span className="mb-1 block text-[12px] font-medium text-muted">Bad ref <span className="text-muted/60">(optional)</span></span>
          <input className={input} placeholder="HEAD" value={bad} onChange={(e) => setBad(e.target.value)} />
        </label>
        <label className="block">
          <span className="mb-1 block text-[12px] font-medium text-muted">Python</span>
          <select className={input} value={python} onChange={(e) => setPython(e.target.value)}>
            {(config?.pythons ?? ["3.11"]).map((p) => <option key={p}>{p}</option>)}
          </select>
        </label>
      </div>
      {err && <p className="rounded-lg border border-bad/40 bg-bad/5 px-3 py-2 text-[13px] text-bad">{err}</p>}
      <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
        <p className="text-[11.5px] text-muted">Every commit runs in a network-less, read-only Docker sandbox. Limits: 15 bisect steps, 5 min per test, 25 min per job.</p>
        <button disabled={busy || STATIC || (config ? !config.nebius_configured : false)} className="rounded-lg bg-nv px-4 py-2 text-[14px] font-semibold text-black hover:bg-[#8ad100] disabled:opacity-50">
          {busy ? "Starting…" : "Start bisect"}
        </button>
      </div>
      {STATIC ? (
        <p className="text-[12px] text-skip">
          Static demo: the verified runs below replay exactly as recorded, including every Nemotron call.{" "}
          {LIVE_URL ? <>Start your own run on the <a className="underline" href={LIVE_URL}>live app</a>.</> : <>To hunt your own regression, run Bisect locally (see the README).</>}
        </p>
      ) : config && !config.nebius_configured && <p className="text-[12px] text-skip">This server has no Token Factory key configured, so new runs are disabled. The demo runs below still work.</p>}
    </form>
  );
}

function RoutingCard({ config }: { config: Config | null }) {
  if (!config) return null;
  const byTier = (t: string) => Object.entries(config.routes).filter(([, v]) => v === t).map(([k]) => k);
  return (
    <section className="rounded-xl border border-line bg-panel p-5">
      <h2 className="text-[15px] font-semibold text-ink">Two-tier model routing</h2>
      <p className="mt-1 text-[12.5px] leading-relaxed text-muted">Every model call goes through one router and is logged with its tier, latency and token count. All inference runs on {config.provider}.</p>
      {(["reason", "fast"] as const).map((t) => (
        <div key={t} className="mt-4">
          <div className="flex items-center gap-2"><TierChip tier={t} /><span className="font-mono text-[11.5px] text-ink/80">{config.tiers[t].model}</span></div>
          <p className="mt-1 text-[12px] text-muted">{t === "reason" ? "Thinking on. Few calls, high value." : "Thinking off. Many calls, must be quick and cheap."}</p>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {byTier(t).map((task) => <span key={task} className="rounded bg-panel2 px-1.5 py-0.5 font-mono text-[10.5px] text-ink/75 ring-1 ring-line">{task}</span>)}
          </div>
        </div>
      ))}
      <div className="mt-4 flex items-center gap-2 border-t border-line pt-3 text-[12px] text-muted">
        <span className="rounded bg-[#2a1f4a] px-1.5 py-[1px] text-[10.5px] font-semibold uppercase text-[#c4b5fd]">Tavily</span>
        searches upstream changelogs, issues and CVEs when the culprit is a dependency bump.
      </div>
    </section>
  );
}

function outcomeChip(o: string, phase: string) {
  if (phase !== "done") return <span className="flex items-center gap-1.5 text-[11.5px] text-nv"><span className="pulse-dot h-1.5 w-1.5 rounded-full bg-nv" />{phase}</span>;
  const c = o === "found" ? "text-good" : o === "ambiguous" ? "text-skip" : "text-bad";
  return <span className={`text-[11.5px] font-semibold ${c}`}>{o}</span>;
}

function RunsList({ runs, onOpen }: { runs: RunSummary[]; onOpen: (id: string) => void }) {
  const seeds = runs.filter((r) => r.seed);
  const rest = runs.filter((r) => !r.seed);
  const Row = ({ r }: { r: RunSummary }) => (
    <button onClick={() => onOpen(r.id)} className="block w-full rounded-lg border border-line bg-panel2 p-3 text-left transition hover:border-nv/50">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[13.5px] font-medium text-ink">{r.title || repoName(r.repo_url)}</span>
        {outcomeChip(r.outcome, r.phase)}
      </div>
      <p className="mt-1 line-clamp-2 text-[12px] text-muted">{r.bug_report}</p>
      {r.first_bad_sha && <p className="mt-1.5 truncate font-mono text-[11.5px]"><span className="text-bad">{short(r.first_bad_sha)}</span> <span className="text-ink/70">{r.first_bad_subject}</span></p>}
    </button>
  );
  return (
    <div className="space-y-5">
      <section>
        <h2 className="mb-1 text-[15px] font-semibold text-ink">Verified demo runs</h2>
        <p className="mb-3 text-[12.5px] text-muted">Real regressions from real projects, pre-run and checked against the maintainers&apos; own fix. Open one to see the full trace instantly, or replay it.</p>
        <div className="grid gap-2.5 md:grid-cols-2">{seeds.map((r) => <Row key={r.id} r={r} />)}</div>
        {!seeds.length && <p className="text-[12.5px] text-muted">No seeded runs loaded.</p>}
      </section>
      {!!rest.length && (
        <section>
          <h2 className="mb-3 text-[15px] font-semibold text-ink">Recent runs</h2>
          <div className="grid gap-2.5 md:grid-cols-2">{rest.slice(0, 12).map((r) => <Row key={r.id} r={r} />)}</div>
        </section>
      )}
    </div>
  );
}

export default function Page() {
  const [runId, go] = useRunParam();
  const [config, setConfig] = useState<Config | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);

  useEffect(() => { getJSON<Config>("/api/config").then(setConfig).catch(() => {}); }, []);
  useEffect(() => { if (!runId) getJSON<RunSummary[]>("/api/runs").then(setRuns).catch(() => {}); }, [runId]);

  return (
    <>
      <Header onHome={() => go(null)} />
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">
        {runId ? (
          <RunView id={runId} onBack={() => go(null)} />
        ) : (
          <div className="space-y-8">
            <section className="max-w-3xl">
              <h1 className="text-3xl font-semibold tracking-tight text-ink sm:text-4xl">Find the commit that broke it.</h1>
              <p className="mt-3 text-[15px] leading-relaxed text-ink/75">
                Give Bisect a public Python repo and a bug report. It writes a failing pytest that reproduces the bug,
                drives <span className="font-mono text-ink">git bisect</span> through the history in a locked-down sandbox,
                explains how the culprit commit causes the symptom, and proposes a verified fix.
              </p>
            </section>
            <div className="grid gap-5 lg:grid-cols-3">
              <div className="lg:col-span-2"><RunForm config={config} onStarted={go} /></div>
              <RoutingCard config={config} />
            </div>
            <RunsList runs={runs} onOpen={go} />
          </div>
        )}
      </main>
      <footer className="mx-auto max-w-7xl px-4 pb-10 pt-4 text-[11.5px] text-muted sm:px-6">
        MIT licensed · <a className="hover:text-ink" href="https://github.com/sja-thedude/bisect-agent">source on GitHub</a> · Nebius x NVIDIA Global AI Hackathon 2026
      </footer>
    </>
  );
}
