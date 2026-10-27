"use client";
import { short, Step } from "@/lib/api";

/** The bisect range as a strip of commits (oldest left). Tested commits light up with their verdict;
 *  commits git has ruled out fade, so the search window visibly halves with every step. */
export function Timeline({ commits, steps, culprit, pendingSha }: {
  commits: string[]; steps: Step[]; culprit?: string; pendingSha?: string;
}) {
  if (!commits.length) return null;
  const pos = new Map(commits.map((c, i) => [c, i]));
  const verdict = new Map(steps.map((s) => [s.sha, s.verdict]));
  let lo = -1;               // newest index known good
  let hi = commits.length - 1; // oldest index known bad (HEAD is bad)
  for (const s of steps) {
    const i = pos.get(s.sha);
    if (i === undefined) continue;
    if (s.verdict === "good") lo = Math.max(lo, i);
    if (s.verdict === "bad") hi = Math.min(hi, i);
  }
  const remaining = Math.max(0, hi - lo);
  const culpritIdx = culprit ? pos.get(culprit) : undefined;
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2 text-[11.5px] text-muted">
        <span>{commits.length} candidate commits · oldest → newest</span>
        <span className="font-mono">
          {culpritIdx !== undefined ? <span className="text-bad">isolated 1 commit</span> : <>search window: <span className="text-ink">{remaining}</span> commits</>}
        </span>
      </div>
      <div className="flex h-9 w-full items-stretch gap-[2px] overflow-hidden rounded-md">
        {commits.map((c, i) => {
          const v = verdict.get(c);
          const inWindow = i > lo && i <= hi;
          const isCulprit = i === culpritIdx;
          const isPending = c === pendingSha;
          const bg = isCulprit ? "bg-bad" : v === "good" ? "bg-good" : v === "bad" ? "bg-bad/70" : v === "skip" ? "bg-skip"
            : isPending ? "bg-nv pulse-dot" : inWindow ? "bg-[#2b3542]" : "bg-[#171c23]";
          return (
            <div key={c} title={`${short(c)}${v ? ` · ${v}` : ""}${isCulprit ? " · FIRST BAD" : ""}`}
              className={`relative min-w-[2px] flex-1 transition-colors duration-500 ${bg} ${isCulprit ? "ring-2 ring-bad ring-offset-1 ring-offset-panel" : ""}`}>
              {(v || isCulprit) && commits.length <= 120 && (
                <span className={`absolute inset-x-0 bottom-0 h-1 ${isCulprit ? "bg-white/70" : "bg-black/20"}`} />
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-muted">
        <Legend c="bg-good" l="good" /><Legend c="bg-bad/70" l="bad" /><Legend c="bg-skip" l="untestable" />
        <Legend c="bg-[#2b3542]" l="still in window" /><Legend c="bg-[#171c23]" l="ruled out" /><Legend c="bg-bad ring-1 ring-bad" l="first bad commit" />
      </div>
    </div>
  );
}

function Legend({ c, l }: { c: string; l: string }) {
  return <span className="flex items-center gap-1.5"><span className={`h-2.5 w-2.5 rounded-sm ${c}`} />{l}</span>;
}
