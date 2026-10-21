import { useState } from "react";
import { api, type SystemInfo } from "../api";
import { Badge, when } from "../format";
import { useApi } from "../useApi";
import SourcesCard from "./SourcesCard";

// =============================================================================
// pages/System.tsx: THE SYSTEM TAB (admin only): THE OPERATOR'S DASHBOARD
// -----------------------------------------------------------------------------
// GET /api/system every 4 seconds (live). Sections, top to bottom:
//   1. four health numbers (queue backlog, workers alive, needs review, failed)
//   2. action buttons (run scoring, retry failed, poll, seed simulation)
//   3. recent scoring runs
//   4. the Sources card (preview/ingest from any source: SourcesCard.tsx)
//   5. the funnel (how many posts in each status) and the workers list
//   6. dead letters (only if there are any)
//   7. read-only configuration
// =============================================================================
// Operator page. Rule: every element answers a question an operator would actually ask,
// and each one says why it matters right next to it.

// Plain-English meaning of each post status, shown in the funnel table.
const STATUS_HELP: Record<string, string> = {
  pending: "waiting for a worker",
  extracted: "produced ideas",
  no_idea: "LLM found no trade idea (questions, news, chatter)",
  needs_review: "waiting for a human decision",
  rejected: "rejected by a reviewer",
  failed: "extraction failed after all retries",
};

export default function System() {
  const { data, error, reload } = useApi<SystemInfo>("/api/system", 4000, true);
  const [msg, setMsg] = useState<string | null>(null);

  // Shared click handler for every action button: POST to `path` (with the admin token),
  // show a message built by `done` from the response, and refresh the page data.
  async function action(path: string, done: (r: Record<string, unknown>) => string) {
    setMsg(null);
    try {
      const r = await api<Record<string, unknown>>(path, { method: "POST", admin: true });
      setMsg(done(r));
      reload();
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }

  if (error) return <section className="card"><div className="error">{error}</div></section>;
  if (!data) return <section className="card"><p className="muted">loading…</p></section>;
  const q = data.queue;
  const c = data.config;
  const pc = data.post_counts;
  const aliveWorkers = data.workers.filter((w) => w.alive).length;
  const lastRun = data.scoring_runs[0];

  return (
    <section className="stack">
      {/* 1. Health at a glance: the four things that need action when they go wrong */}
      <div className="grid-4">
        <Stat label="waiting in queue" value={q.waiting}
              why={q.oldest_waiting_s ? `oldest ${Math.round(q.oldest_waiting_s)}s. A growing backlog means workers are down or the LLM is rate-limited`
                : "work not yet picked up; should drain within minutes"} warn={(q.oldest_waiting_s ?? 0) > 600} />
        <Stat label="workers alive" value={aliveWorkers}
              why={aliveWorkers === 0 ? "nothing is processing the queue: run serve (embedded) or tally worker" : "processes currently draining the queue"}
              warn={aliveWorkers === 0} />
        <Stat label="needs review" value={pc.needs_review ?? 0}
              why="posts the system didn't trust; handled in the Review tab" warn={(pc.needs_review ?? 0) > 0} />
        <Stat label="failed" value={pc.failed ?? 0}
              why="exhausted retries (bad key, retired model, outage). Fix the cause, then retry below" warn={(pc.failed ?? 0) > 0} />
      </div>
      {q.error && <div className="error">queue unreachable: {q.error}</div>}

      <div className="grid-2">
        {/* 2. Actions: each exists because waiting for the schedule isn't always acceptable */}
        <div className="card">
          <h3>Actions</h3>
          <Action label="Run scoring now"
                  why={`Leaderboard, ideas and signals only update when scoring runs${c.scoring_interval_minutes ? ` (every ${c.scoring_interval_minutes / 60} h)` : ""}. Use after approving ideas or for a demo.`}
                  onClick={() => action("/api/admin/score", () => "Scoring started; the run appears below when it finishes.")} />
          <Action label={`Retry failed posts (${pc.failed ?? 0})`} disabled={!pc.failed}
                  why="Re-queues posts whose extraction failed permanently, after you've fixed the cause (e.g. the Groq model retirement)."
                  onClick={() => action("/api/admin/retry-failed", (r) => `${r.requeued} post(s) re-queued.`)} />
          <Action label="Poll scheduled sources" disabled={c.sources.length === 0}
                  why={c.sources.length ? `Run the scheduled poll now instead of waiting: ${c.sources.join(", ")}.`
                    : "Nothing scheduled in .env. Use the Sources card below to pull from any source on demand."}
                  onClick={() => action("/api/admin/poll", () => "Polling started.")} />
          <Action label="Seed simulated contributors" disabled={data.simulated_seeded}
                  why={data.simulated_seeded ? "Already seeded. Simulated contributors exist (tagged 'sim' on the leaderboard)."
                    : "One-off: creates contributors with known skill to validate the scoring maths and populate the demo."}
                  onClick={() => action("/api/admin/simulate", () => "Seeding + scoring started (takes a minute).")} />
          {msg && <div className="notice">{msg}</div>}
        </div>

        {/* 3. Is scoring fresh? Every number on the public pages is only as new as the last run */}
        <div className="card">
          <h3>Scoring runs</h3>
          <p className="small muted">Public pages are only as fresh as the last successful run.
            {lastRun ? ` Last run: ${when(lastRun.started_at)}.` : " No run yet."}</p>
          {data.scoring_runs.length > 0 && (
            <table className="tbl"><thead><tr><th>started</th><th className="r">ideas marked</th><th className="r">contributors</th><th>result</th></tr></thead>
              <tbody>{data.scoring_runs.map((r) => (
                <tr key={r.id}><td className="small">{when(r.started_at)}</td><td className="num">{r.ideas_marked}</td>
                  <td className="num">{r.contributors_scored}</td>
                  <td>{r.error ? <span title={r.error}><Badge kind="bad">error</Badge></span> : r.finished_at ? <Badge kind="ok">ok</Badge> : <Badge kind="info">running</Badge>}</td></tr>
              ))}</tbody></table>
          )}
        </div>
      </div>

      {/* Sources: where posts come from, with a picker to preview and pull on demand */}
      <SourcesCard />

      <div className="grid-2">
        {/* 4. The extraction funnel: where posts end up, and so where attention is needed */}
        <div className="card">
          <h3>Where posts end up</h3>
          <p className="small muted">The extraction funnel: how much is noise, how much becomes ideas, how much needs a human.</p>
          <table className="tbl"><tbody>
            {Object.entries(STATUS_HELP).filter(([k]) => pc[k]).map(([k, help]) => (
              <tr key={k}><td><Badge kind={k === "failed" ? "bad" : k === "needs_review" ? "warn" : k === "extracted" ? "ok" : "muted"}>{k.replace("_", " ")}</Badge></td>
                <td className="num">{pc[k]}</td><td className="small muted">{help}</td></tr>
            ))}
          </tbody></table>
        </div>

        {/* 5. Workers: alive or not, and are they failing */}
        <div className="card">
          <h3>Workers (last 24 h)</h3>
          <p className="small muted">If none are alive, posts queue up but never get extracted. A high failed count points at the LLM provider.</p>
          {data.workers.length === 0 ? <p className="muted small">No worker has run in the last 24 h.</p> : (
            <table className="tbl"><thead><tr><th>worker</th><th className="r">done</th><th className="r">failed</th><th>last seen</th></tr></thead>
              <tbody>{data.workers.map((w) => (
                <tr key={w.worker_id}><td className="mono small">{w.alive ? "● " : "○ "}{w.worker_id}</td>
                  <td className="num">{w.processed}</td><td className="num">{w.failed}</td><td className="small">{when(w.last_seen)}</td></tr>
              ))}</tbody></table>
          )}
        </div>
      </div>

      {/* 6. Dead letters: shown only when there are any, because they're the "why did it fail" */}
      {data.dead_letters.length > 0 && (
        <div className="card">
          <h3>Failed jobs (dead letters)</h3>
          <p className="small muted">Jobs that failed {c.max_attempts} times. The error says what to fix before using "Retry failed posts".</p>
          <table className="tbl"><thead><tr><th>post</th><th className="r">attempts</th><th>last error</th></tr></thead>
            <tbody>{data.dead_letters.map((d) => (
              <tr key={d.id}><td className="mono small">#{d.payload?.post_id ?? "?"}</td>
                <td className="num">{d.attempts}</td><td className="small">{d.error}</td></tr>
            ))}</tbody></table>
        </div>
      )}

      {/* 7. Configuration: read-only on purpose */}
      <div className="card">
        <h3>Configuration (read-only)</h3>
        <p className="small muted">Shown because each value explains behaviour you'll see above. It's read-only on purpose:
          settings live in <code>.env</code>, are read at startup, and include secrets, so changing them from a web page would be a security risk.</p>
        <dl className="stats cfg">
          <Cfg k="Extraction model" v={c.extractor} why="Which LLM turns posts into ideas." />
          <Cfg k="LLM rate limit" v={c.llm_requests_per_minute ? `${c.llm_requests_per_minute}/min` : "n/a"} why="Caps throughput. Explains a slowly draining queue." />
          <Cfg k="Review threshold" v={`confidence < ${c.review_confidence_threshold}`} why="Below this, extractions go to Review." />
          <Cfg k="Market data" v={c.market_data} why={c.market_data === "synthetic" ? "FAKE prices. Results are not real." : "Real Yahoo Finance prices."} warn={c.market_data === "synthetic"} />
          <Cfg k="Queue" v={c.queue_backend} why="Where jobs wait (Postgres or Redis)." />
          <Cfg k="Retry policy" v={`${c.max_attempts} attempts, ${c.lease_seconds}s lease`} why="A crashed worker's job is retried after the lease expires, and fails permanently after the last attempt." />
        </dl>
      </div>
    </section>
  );
}

// Small building blocks used above, so each section's markup stays short:
//   Stat   = a big number + label + explanation (orange border if warn)
//   Action = a button + why you'd press it
//   Cfg    = one configuration value + what it explains
function Stat({ label, value, why, warn }: { label: string; value: number | null | undefined; why: string; warn?: boolean }) {
  return (
    <div className={`card stat ${warn ? "warn" : ""}`}>
      <div className="stat-value">{value ?? "—"}</div>
      <div className="stat-label">{label}</div>
      <div className="small muted">{why}</div>
    </div>
  );
}

function Action({ label, why, onClick, disabled }: { label: string; why: string; onClick: () => void; disabled?: boolean }) {
  return (
    <div className="action">
      <button onClick={onClick} disabled={disabled}>{label}</button>
      <span className="small muted">{why}</span>
    </div>
  );
}

function Cfg({ k, v, why, warn }: { k: string; v: string; why: string; warn?: boolean }) {
  return (
    <div><dt>{k}</dt><dd className={warn ? "neg" : ""}>{v}</dd><div className="small muted">{why}</div></div>
  );
}
