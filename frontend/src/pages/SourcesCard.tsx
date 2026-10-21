// =============================================================================
// pages/SourcesCard.tsx: THE "SOURCES" CARD ON THE SYSTEM TAB
// -----------------------------------------------------------------------------
// Pick a source type (Reddit / Substack / StockTwits / Other RSS) and a target, then:
//   Preview (free) -> POST /api/sources/preview : fetch + keep/drop labels; saves nothing, no LLM
//   Ingest         -> POST /api/sources/ingest  : fetch, pre-filter, save + queue the kept posts
// Ingest stays disabled until you've previewed, so you always know what you're about to
// spend LLM calls on.
// =============================================================================
import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge } from "../format";
import { useApi } from "../useApi";

// Shapes of what the three endpoints return.
type Kind = "reddit" | "substack" | "stocktwits" | "rss";
type SourcesInfo = { scheduled: string[]; suggested: Record<Kind, string[]>; poll_interval_minutes: number };
type PreviewItem = { author: string; text: string; url: string | null; keep: boolean; author_sentiment: string | null };
type Preview = { source: string; fetched: number; kept: number; items: PreviewItem[] };
type IngestReport = { source: string; new: number; duplicate: number; filtered: number; errors: number };

// The four buttons: what to call the target box, and a one-line explanation of each.
const KINDS: { id: Kind; label: string; targetLabel: string; why: string }[] = [
  { id: "reddit", label: "Reddit", targetLabel: "subreddit",
    why: "Forum posts, read through Reddit's public RSS feed (its JSON API now blocks anonymous access)." },
  { id: "substack", label: "Substack", targetLabel: "publication name",
    why: "Newsletters. Type the name from <name>.substack.com; Tally reads its feed (full articles, not just subtitles)." },
  { id: "stocktwits", label: "StockTwits", targetLabel: "symbol or 'trending'",
    why: "A social network built for stock ideas. Currently blocked by its bot protection (403); needs the official API." },
  { id: "rss", label: "Other RSS", targetLabel: "feed URL (https)",
    why: "Any other blog or site that publishes an RSS/Atom feed." },
];

// Pick a source, dry-run it (free: no saving, no LLM), then ingest the kept posts.
export default function SourcesCard() {
  const { data } = useApi<SourcesInfo>("/api/sources", undefined, true);
  const [kind, setKind] = useState<Kind>("reddit");
  const [target, setTarget] = useState("stocks");
  const [limit, setLimit] = useState(10);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [report, setReport] = useState<IngestReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const meta = KINDS.find((k) => k.id === kind)!;
  const suggestions = data?.suggested[kind] ?? [];

  useEffect(() => {   // switching source type resets the target to its first suggestion
    setTarget(data?.suggested[kind]?.[0] ?? ""); setPreview(null); setReport(null); setErr(null);
  }, [kind, data]);

  // One function for both buttons: same request body, different endpoint and result slot.
  async function run(path: "preview" | "ingest") {
    setBusy(true); setErr(null); setReport(null);
    try {
      const body = JSON.stringify({ kind, target: target.trim(), limit });
      if (path === "preview") setPreview(await api<Preview>("/api/sources/preview", { method: "POST", body, admin: true }));
      else setReport(await api<IngestReport>("/api/sources/ingest", { method: "POST", body, admin: true }));
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="card">
      <h3>Sources</h3>
      <p className="small muted">
        <b>Scheduled</b> (polled every {data?.poll_interval_minutes ?? "…"} min, set in <code>.env</code>):{" "}
        {data?.scheduled.length ? data.scheduled.map((s) => <Badge key={s} kind="muted">{s}</Badge>) : "none, so posts only arrive when you pull them here or submit them."}
      </p>

      <div className="row wrap">
        {KINDS.map((k) => (
          <button key={k.id} className={kind === k.id ? "primary" : ""} onClick={() => setKind(k.id)}>{k.label}</button>
        ))}
      </div>
      <p className="small muted">{meta.why}</p>

      <div className="row wrap">
        <label className="field">{meta.targetLabel}
          <input list={`suggest-${kind}`} value={target} onChange={(e) => setTarget(e.target.value)}
                 size={kind === "rss" ? 44 : 18} className="mono" />
          <datalist id={`suggest-${kind}`}>{suggestions.map((s) => <option key={s} value={s} />)}</datalist>
        </label>
        <label className="field">posts
          <input type="number" min={1} max={25} value={limit} style={{ width: 60 }}
                 onChange={(e) => setLimit(Math.max(1, Math.min(25, Number(e.target.value) || 1)))} />
        </label>
        <button onClick={() => run("preview")} disabled={busy || !target.trim()}>Preview (free)</button>
        <button className="primary" onClick={() => run("ingest")} disabled={busy || !target.trim() || !preview}
                title={preview ? "" : "Preview first so you know what you're ingesting"}>
          Ingest {preview ? `${preview.kept} kept` : ""}
        </button>
      </div>
      {kind === "reddit" && (
        <div className="pills" style={{ marginTop: 8 }}>
          {suggestions.map((s) => <button key={s} className="chip" onClick={() => setTarget(s)}>r/{s}</button>)}
        </div>
      )}
      <p className="small muted">Preview saves nothing and calls no LLM. Ingest saves the kept posts and queues them; each one
        costs one LLM call when a worker picks it up. Re-ingesting the same posts is harmless (deduplicated).</p>

      {err && <div className="error">{err}</div>}
      {report && (
        <div className="notice">{report.source}: {report.new} new post(s) queued, {report.duplicate} already seen,{" "}
          {report.filtered} dropped by the keyword pre-filter{report.errors ? `, ${report.errors} error(s)` : ""}.
          {report.new > 0 && " Watch them on the Review/Ideas tabs once a worker has processed them."}</div>
      )}
      {preview && (
        <>
          <p className="small"><b>{preview.source}</b>: {preview.fetched} fetched, <b>{preview.kept}</b> would be kept
            (mention a known stock), so up to {preview.kept} LLM call(s).</p>
          <table className="tbl"><tbody>
            {preview.items.map((i, n) => (
              <tr key={n}>
                <td>{i.keep ? <Badge kind="ok">keep</Badge> : <Badge kind="muted">drop</Badge>}</td>
                <td className="mono small">{i.author}{i.author_sentiment && <> <Badge kind={i.author_sentiment === "Bullish" ? "ok" : "bad"}>{i.author_sentiment}</Badge></>}</td>
                <td className="small">{i.url ? <a href={i.url} target="_blank" rel="noreferrer">{i.text.slice(0, 140)}</a> : i.text.slice(0, 140)}</td>
              </tr>
            ))}
          </tbody></table>
        </>
      )}
    </div>
  );
}
