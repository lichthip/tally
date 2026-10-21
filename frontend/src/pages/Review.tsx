import { useState } from "react";
import { api, type Post } from "../api";
import { Dir, when } from "../format";
import { useApi } from "../useApi";

type Row = { ticker: string; direction: string; horizon_days: number | "" };
type ApproveResult = { post_id: number; ideas_created: number; effective_at: string };

export default function Review() {
  const { data, error, reload } = useApi<Post[]>("/api/review", undefined, true);
  const [log, setLog] = useState<string[]>([]);
  const done = (msg: string) => { setLog((l) => [msg, ...l].slice(0, 5)); reload(); };

  return (
    <section className="card">
      <h2>Review queue</h2>
      <p className="lede">Extractions the system didn't trust: an unknown ticker, low model confidence, malformed output, or
        <b> the LLM and the keyword baseline disagreeing</b>. Edit the tickers, directions and horizons, or add and remove rows,
        then approve or reject. Approved ideas are priced from the <b>approval time</b>, not from when the post arrived, so a
        reviewer can't use hindsight. Approving needs the admin token.</p>
      {log.map((m, i) => <div key={i} className="notice">{m}</div>)}
      {error && <div className="error">{error}</div>}
      {data && data.length === 0 && <p className="muted">Nothing to review.</p>}
      {data?.map((p) => <ReviewItem key={p.id} post={p} onDone={done} />)}
    </section>
  );
}

function ReviewItem({ post, onDone }: { post: Post; onDone: (msg: string) => void }) {
  const cands = post.extraction?.candidates ?? [];
  const [rows, setRows] = useState<Row[]>(
    cands.length
      ? cands.map((c) => ({ ticker: c.ticker ?? "", direction: c.direction, horizon_days: c.horizon_days }))
      : [{ ticker: "", direction: "LONG", horizon_days: 20 }]);
  const [msg, setMsg] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const update = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const remove = (i: number) => setRows(rows.filter((_, j) => j !== i));
  const add = () => setRows([...rows, { ticker: "", direction: "LONG", horizon_days: 20 }]);
  const valid = rows.filter((r) => r.ticker.trim());

  async function approve() {
    setMsg(null);
    if (valid.length === 0) { setMsg("Add at least one ticker, or reject the post instead."); return; }
    try {
      const overrides = valid.map((r) => ({
        ticker: r.ticker.trim(), direction: r.direction,
        horizon_days: r.horizon_days === "" ? null : Number(r.horizon_days),
      }));
      const res = await api<ApproveResult>(`/api/review/${post.id}/approve`,
        { method: "POST", body: JSON.stringify({ overrides }), admin: true });
      onDone(`#${post.id} approved: ${res.ideas_created} idea(s) created (${valid.map((r) => `${r.ticker} ${r.direction}`).join(", ")}). `
        + `Entry = first market open after ${when(res.effective_at)}; it appears on the Ideas tab after the next scoring run.`);
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }

  async function reject() {
    setMsg(null);
    try {
      await api(`/api/review/${post.id}/reject`, { method: "POST", admin: true });
      onDone(`#${post.id} rejected: no ideas created. The post stays in the database for audit.`);
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }

  return (
    <div className="review">
      <div className="review-text">
        <div className="muted small">#{post.id} · {post.contributor} · {when(post.received_at)} · {post.extractor}</div>
        <ul className="notes">{post.extraction?.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        <blockquote className={expanded ? "" : "clamp"}>{post.text}</blockquote>
        {post.text.length > 400 && (
          <button className="link" onClick={() => setExpanded(!expanded)}>{expanded ? "show less" : "show full post"}</button>
        )}
      </div>
      <div className="review-form">
        <div className="row small muted header-row"><span style={{ width: 96 }}>ticker</span><span style={{ width: 86 }}>direction</span><span>horizon (trading days)</span></div>
        {rows.map((r, i) => (
          <div className="row" key={i}>
            <input value={r.ticker} placeholder="TICKER" size={8} className="mono"
                   onChange={(e) => update(i, { ticker: e.target.value.toUpperCase() })} />
            <select value={r.direction} onChange={(e) => update(i, { direction: e.target.value })}>
              <option>LONG</option><option>SHORT</option>
            </select>
            <input type="number" min={1} max={252} value={r.horizon_days} style={{ width: 70 }}
                   onChange={(e) => update(i, { horizon_days: e.target.value === "" ? "" : Number(e.target.value) })} />
            <button className="icon" title="remove" onClick={() => remove(i)}>×</button>
            {cands[i] && <span className="small muted">suggested <Dir d={cands[i].direction} /> conf {cands[i].confidence.toFixed(2)}</span>}
          </div>
        ))}
        <div className="row"><button className="link" onClick={add}>+ add ticker</button></div>
        <div className="row">
          <button className="primary" onClick={approve}>Approve {valid.length > 0 ? `${valid.length} idea${valid.length > 1 ? "s" : ""}` : ""}</button>
          <button onClick={reject}>Reject</button>
        </div>
        {msg && <div className="error">{msg}</div>}
      </div>
    </div>
  );
}
