// =============================================================================
// pages/Leaderboard.tsx: THE LEADERBOARD TAB
// -----------------------------------------------------------------------------
// GET /api/leaderboard?sort=...&include_simulated=... and show one row per contributor:
// closed/open counts, hit rate, RAW mean excess, SHRUNK skill estimate, a 95% interval
// bar, and P(skill > 0). Switching "rank by" between raw and shrunk is the demo moment:
// small-sample accounts drop once shrinkage is applied.
// =============================================================================
import { useState } from "react";
import type { LeaderRow } from "../api";
import { Badge, Ret, num, pct } from "../format";
import { useApi } from "../useApi";

export default function Leaderboard() {
  const [sort, setSort] = useState("shrunk");
  const [sim, setSim] = useState(true);
  // Changing sort or sim changes the URL string, so useApi re-fetches automatically.
  const { data, error, loading } = useApi<LeaderRow[]>(`/api/leaderboard?sort=${sort}&include_simulated=${sim}`);

  // The widest interval end (in absolute value) across all rows, so every bar uses the
  // same scale. flatMap = map then flatten; ...array spreads the values into Math.max.
  const span = Math.max(0.01, ...(data ?? []).flatMap((r) => (r.ci95 ? [Math.abs(r.ci95[0]), Math.abs(r.ci95[1])] : [])));

  return (
    <section className="card">
      <div className="head-row">
        <h2>Leaderboard</h2>
        <div className="controls">
          <label className="field">rank by
            <select value={sort} onChange={(e) => setSort(e.target.value)}>
              <option value="shrunk">skill estimate (shrunk)</option>
              <option value="raw">raw mean excess</option>
              <option value="hit_rate">hit rate</option>
              <option value="n">ideas closed</option>
            </select>
          </label>
          <label className="check"><input type="checkbox" checked={sim} onChange={(e) => setSim(e.target.checked)} /> simulated</label>
        </div>
      </div>
      <p className="lede">Returns are <b>excess over the benchmark</b>, normalised to a 20-day horizon. The skill estimate is
        empirical-Bayes <b>shrunk toward zero</b>: a 3-for-3 streak barely moves it, and a long record does. The bar shows the 95% interval.
        Try ranking by raw mean, then by skill estimate, and see where <span className="mono">sim_lucky_streak</span> lands.</p>
      {error && <div className="error">{error}</div>}
      {loading && !data && <p className="muted">loading…</p>}
      {data && data.length === 0 && <p className="muted">No scored contributors yet. Seed the simulation from the System tab, or submit ideas and run scoring.</p>}
      {data && data.length > 0 && (
        <table className="tbl">
          <thead>
            <tr><th>#</th><th>contributor</th><th className="r">closed</th><th className="r">open</th><th className="r">hit rate</th>
              <th className="r">raw mean</th><th className="r">skill est.</th><th style={{ width: 220 }}>95% interval</th><th className="r">P(skill&gt;0)</th></tr>
          </thead>
          <tbody>
            {data.map((r, i) => (
              <tr key={r.contributor_id}>
                <td className="muted">{i + 1}</td>
                <td><span className="mono">{r.handle}</span>{" "}
                  {r.is_simulated && <Badge kind="muted">sim · true acc {pct(r.true_accuracy, 0)}</Badge>}
                  {r.n_unscorable > 0 && <Badge kind="warn">{r.n_unscorable} unscorable</Badge>}</td>
                <td className="num">{r.n_closed}</td>
                <td className="num">{r.n_open}</td>
                <td className="num">{pct(r.hit_rate, 0)}</td>
                <td><Ret value={r.mean_excess} /></td>
                <td><Ret value={r.posterior_mean} /></td>
                <td><CiBar ci={r.ci95} mean={r.posterior_mean} span={span} /></td>
                <td className="num">{num(r.prob_positive)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// A tiny chart drawn with plain divs: a zero line in the middle, a coloured range from
// the interval's low to high end, and a dot at the estimate. If the range doesn't cross
// zero, we're fairly sure about the skill's sign.
function CiBar({ ci, mean, span }: { ci: [number, number] | null; mean: number | null; span: number }) {
  if (!ci || mean === null) return <span className="muted">—</span>;
  // Map a value in [-span, +span] to a CSS left-position in [0%, 100%] (0 -> 50%, the centre).
  const x =(v: number) => 50 + (Math.max(-span, Math.min(span, v)) / span) * 50;
  return (
    <div className="ci">
      <div className="ci-zero" />
      <div className={`ci-range ${mean >= 0 ? "pos" : "neg"}`} style={{ left: `${x(ci[0])}%`, width: `${x(ci[1]) - x(ci[0])}%` }} />
      <div className="ci-dot" style={{ left: `${x(mean)}%` }} />
    </div>
  );
}
