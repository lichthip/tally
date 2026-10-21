import { useEffect, useRef, useState } from "react";
import { api, type IngestResult, type Post } from "../api";
import { Badge, Dir, Ret, day, when } from "../format";

const EXAMPLES = [
  "Loading up on $NVDA here, datacenter demand isn't slowing. 200 by March.",
  "Grabbed some TSLA puts before deliveries, they're going to disappoint",
  "Selling cash-secured puts on AAPL at 180, happy to own it there",
  "Long AMD, short INTC as a pair trade into Q4",
  "DBS looks cheap at these levels, accumulating for the dividends",
  "Oh sure, Intel is definitely going to $200 next month. Totally.",
  "Should I buy Palantir at these levels?",
];

const STAGES = ["received", "queued", "extracting", "done"] as const;

export default function Submit() {
  const [text, setText] = useState(EXAMPLES[0]);
  const [author, setAuthor] = useState("me");
  const [result, setResult] = useState<IngestResult | null>(null);
  const [post, setPost] = useState<Post | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current); }, []);

  async function submit() {
    setError(null); setPost(null); setResult(null); setBusy(true);
    if (timer.current) window.clearInterval(timer.current);
    try {
      const r = await api<IngestResult>("/api/posts", {
        method: "POST",
        body: JSON.stringify({ text, author }),
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      setResult(r);
      const started = Date.now();
      const poll = async () => {
        const p = await api<Post>(`/api/posts/${r.post_id}`);
        setPost(p);
        if (p.status !== "pending" || Date.now() - started > 120_000) {
          if (timer.current) window.clearInterval(timer.current);
          setBusy(false);
        }
      };
      await poll();
      timer.current = window.setInterval(poll, 1000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  // index of the stage in progress; STAGES.length means everything finished
  const stage = !result ? -1 : !post ? 1 : post.status === "pending" ? 2 : STAGES.length;

  return (
    <section className="grid-2">
      <div className="card">
        <h2>Submit a post</h2>
        <p className="lede">Paste a post the way you'd see it on Reddit, X or in a newsletter. It's stored with
          <em> our</em> timestamp, queued, and turned into structured trade ideas by the extraction worker.</p>
        <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} maxLength={5000} />
        <div className="row">
          <label className="field">author <input value={author} onChange={(e) => setAuthor(e.target.value)} /></label>
          <button className="primary" onClick={submit} disabled={busy || text.trim().length < 3}>
            {busy ? "Processing…" : "Submit"}
          </button>
        </div>
        <div className="examples">
          <span className="muted">Try:</span>
          {EXAMPLES.map((ex) => (
            <button key={ex} className="chip" onClick={() => setText(ex)} title={ex}>{ex.slice(0, 38)}…</button>
          ))}
        </div>
        {error && <div className="error">{error}</div>}
      </div>

      <div className="card">
        <h2>Pipeline</h2>
        <ol className="stages">
          {STAGES.map((s, i) => (
            <li key={s} className={i < stage ? "done" : i === stage ? "now" : ""}>{s}</li>
          ))}
        </ol>
        {result && (
          <p className="kv">post <b>#{result.post_id}</b> {result.duplicate && <Badge kind="warn">duplicate: deduplicated</Badge>}
            {result.job_id && <span className="muted"> · job {result.job_id}</span>}</p>
        )}
        {post && post.status !== "pending" && <Outcome post={post} />}
        {!result && <p className="muted">Submit something to watch it flow through API → queue → worker → database.</p>}
      </div>
    </section>
  );
}

function Outcome({ post }: { post: Post }) {
  const ex = post.extraction;
  const kind = post.status === "extracted" ? "ok" : post.status === "needs_review" ? "warn" : post.status === "failed" ? "bad" : "muted";
  return (
    <div className="outcome">
      <p><Badge kind={kind}>{post.status.replace("_", " ")}</Badge>
        <span className="muted"> by {post.extractor}{ex?.cached ? " (cache hit)" : ""} · received {when(post.received_at)}</span></p>
      {ex?.notes?.length ? <ul className="notes">{ex.notes.map((n, i) => <li key={i}>{n}</li>)}</ul> : null}
      {post.error && <div className="error">{post.error}</div>}
      {post.ideas && post.ideas.length > 0 && (
        <table className="tbl">
          <thead><tr><th>ticker</th><th>dir</th><th>horizon</th><th>conf.</th><th>entry</th><th>excess</th></tr></thead>
          <tbody>
            {post.ideas.map((i) => (
              <tr key={i.id}>
                <td className="mono">{i.ticker}</td><td><Dir d={i.direction} /></td>
                <td className="num">{i.horizon_days}d{i.horizon_inferred && <span className="muted">*</span>}</td>
                <td className="num">{i.confidence.toFixed(2)}</td>
                <td className="num">{i.mark ? day(i.mark.entry_date) : <span className="muted">next scoring run</span>}</td>
                <td><Ret value={i.mark?.excess_return} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {post.status === "needs_review" && ex?.candidates?.length ? (
        <p className="muted">Candidates are waiting in the Review tab. A human approves or rejects them, and approved
          ideas are priced from the approval time, so the reviewer can't use hindsight.</p>
      ) : null}
      {post.ideas?.some((i) => i.horizon_inferred) && <p className="muted small">* no horizon stated, default used</p>}
    </div>
  );
}
