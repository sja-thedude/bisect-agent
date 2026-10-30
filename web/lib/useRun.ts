"use client";
import { useEffect, useRef, useState } from "react";
import { API, getJSON, RunEvent, RunState, STATIC } from "./api";

const REFRESH_KINDS = new Set(["phase", "step", "repro", "repro_confirmed", "first_bad", "explanation", "fix",
  "verification", "report", "research", "research_decision", "counterfactual", "end"]);

/** Follows a run: recorded events stream over SSE (live runs tail, finished runs replay instantly
 *  or paced with `speed`), and the full state is refetched whenever something structural changes. */
export function useRun(id: string | null, speed: number, nonce: number) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [state, setState] = useState<RunState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!id) return;
    let closed = false;
    setEvents([]); setError(null);
    const refresh = () => {
      if (timer.current) return;
      timer.current = setTimeout(() => {
        timer.current = null;
        getJSON<RunState>(`/api/runs/${id}`).then((s) => !closed && setState(s)).catch(() => {});
      }, 300);
    };
    getJSON<RunState>(`/api/runs/${id}`).then((s) => {
      if (closed) return;
      // When replaying a finished run, hide the final state until the replay gets there.
      setState(speed > 0 ? { ...s, phase: "clone", steps: [], calls: [], explanation: {}, fix: {}, first_bad: {}, research: {} } : s);
    }).catch((e) => setError(String(e.message ?? e)));
    if (STATIC) {
      // No server: fetch the recorded run and replay its events locally (instantly or paced).
      const timers: ReturnType<typeof setTimeout>[] = [];
      fetch(`/demo/runs/${id}.json`).then((r) => { if (!r.ok) throw new Error(r.status === 404 ? "run not found" : `HTTP ${r.status}`); return r.json(); }).then((data: { state: RunState; events: RunEvent[] }) => {
        if (closed) return;
        if (speed === 0) { setEvents(data.events); setState({ ...data.state, seed: true }); setStreaming(false); return; }
        setStreaming(true);
        let t = 0;
        data.events.forEach((ev, i) => {
          if (i > 0) t += Math.min(2000, Math.max(0, ((ev.ts - data.events[i - 1].ts) * 1000) / speed));
          timers.push(setTimeout(() => {
            if (closed) return;
            setEvents((prev) => prev.concat(ev));
            if (ev.kind === "report") { setState({ ...ev.payload.state, seed: true }); setStreaming(false); }
          }, t));
        });
      }).catch((e) => setError(String(e.message ?? e)));
      return () => { closed = true; timers.forEach(clearTimeout); };
    }
    const es = new EventSource(`${API}/api/runs/${id}/events?replay_speed=${speed}`);
    setStreaming(true);
    const batch: RunEvent[] = [];
    const seen = new Set<number>();
    let flush: ReturnType<typeof setTimeout> | null = null;
    const onAny = (e: MessageEvent) => {
      const ev: RunEvent = JSON.parse(e.data);
      if (seen.has(ev.seq)) return; // a reconnect must never duplicate the log
      seen.add(ev.seq);
      batch.push(ev);
      if (!flush) flush = setTimeout(() => { setEvents((prev) => prev.concat(batch.splice(0))); flush = null; }, 60);
      if (REFRESH_KINDS.has(ev.kind) && speed === 0) refresh();
      if (speed > 0 && ev.kind === "report") setState(ev.payload.state);
    };
    const kinds = ["log", "phase", "call", "step", "step_start", "repro", "repro_confirmed", "first_bad", "explanation",
      "fix", "verification", "report", "search", "research", "research_decision", "range", "counterfactual"];
    kinds.forEach((k) => es.addEventListener(k, onAny as EventListener));
    es.addEventListener("end", () => { es.close(); setStreaming(false); refresh(); });
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) setStreaming(false); };
    return () => { closed = true; es.close(); setStreaming(false); };
  }, [id, speed, nonce]);

  return { events, state, error, streaming };
}
