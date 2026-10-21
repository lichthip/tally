import { useState } from "react";
import type { Idea } from "../api";
import { Badge, Dir, Ret, day, num } from "../format";
import { useApi } from "../useApi";

const STATUS_KIND: Record<string, string> = { closed: "ok", open: "info", pending_entry: "muted", unscorable: "warn" };

export default function Ideas() {
  const [status, setStatus] = useState("");
  const [ticker, setTicker] = useState("");
  const [sim, setSim] = useState(false);
  const qs = new URLSearchParams({ limit: "200", include_simulated: String(sim) });
  if (status) qs.set("mark_status", status);
  if (ticker) qs.set("ticker", ticker);
  const { data, error } = useApi<Idea[]>(`/api/ideas?${qs}`);

  return (
    <section className="card">
      <div className="head-row">
        <h2>Ideas</h2>
        <div className="controls">
          <label className="field">status
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">all</option><option value="open">open</option><option value="closed">closed</option>
              <option value="pending_entry">pending entry</option><option value="unscorable">unscorable</option>
            </select>
          </label>
          <label className="field">ticker <input value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())} size={8} /></label>
          <label className="check"><input type="checkbox" checked={sim} onChange={(e) => setSim(e.target.checked)} /> simulated</label>
        </div>
      </div>
      <p className="lede">The append-only idea log. The entry is the first <b>open</b> after we received the idea, in the stock's own
        exchange timezone. Excess = direction × (stock − benchmark) over the same dates.</p>
      {error && <div className="error">{error}</div>}
      {data && (
        <table className="tbl">
          <thead><tr><th>received</th><th>contributor</th><th>ticker</th><th>dir</th><th className="r">horizon</th>
            <th>entry</th><th className="r">entry px</th><th>marked</th><th className="r">stock</th><th className="r">bench</th>
            <th className="r">excess</th><th>status</th></tr></thead>
          <tbody>
            {data.map((i) => (
              <tr key={i.id} title={i.rationale ?? ""}>
                <td className="mono small">{day(i.effective_at)}</td>
                <td className="mono">{i.contributor}</td>
                <td className="mono">{i.ticker}</td>
                <td><Dir d={i.direction} /></td>
                <td className="num">{i.horizon_days}d</td>
                <td className="mono small">{day(i.mark?.entry_date)}</td>
                <td className="num">{num(i.mark?.entry_price)}</td>
                <td className="mono small">{day(i.mark?.mark_date)}</td>
                <td><Ret value={i.mark?.raw_return} /></td>
                <td><Ret value={i.mark?.bench_return} /></td>
                <td><Ret value={i.mark?.excess_return} /></td>
                <td>{i.mark ? <Badge kind={STATUS_KIND[i.mark.status]}>{i.mark.status.replace("_", " ")}</Badge> : <Badge kind="muted">unmarked</Badge>}
                  {i.mark?.unscorable_reason && <div className="small muted">{i.mark.unscorable_reason}</div>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data && data.length === 0 && <p className="muted">No ideas match.</p>}
    </section>
  );
}
