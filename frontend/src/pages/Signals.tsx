// =============================================================================
// pages/Signals.tsx: THE SIGNALS TAB
// -----------------------------------------------------------------------------
// Shows the newest signal snapshot (GET /api/signals), written by the last scoring run:
//   left:  per-ticker skill-weighted direction (a meter from -1 to +1), conviction, and
//          the paper portfolio's weight in that ticker
//   right: the portfolio's risk (VaR, CVaR, volatility) and the skill model's sigma/tau
// =============================================================================
import type { SignalsPayload } from "../api";
import { num, pct, signedPct, when } from "../format";
import { useApi } from "../useApi";

export default function Signals() {
  const { data, error } = useApi<SignalsPayload>("/api/signals");
  // "Early returns": handle the error and loading cases first, so the main markup below
  // can assume data exists.
  if (error) return <section className="card"><div className="error">{error}</div></section>;
  if (!data) return <section className="card"><p className="muted">loading…</p></section>;
  // Only tickers with SOME skilled weight behind them (unproven-only tickers are hidden).
  const live = data.signals.filter((s) => s.weight_mass > 0);

  // The meter: a bar growing right from the centre for positive scores, left for negative.
  // sigma and tau are stored squared (variances), hence Math.sqrt for display.
  return (
    <section className="grid-2 wide-left">
      <div className="card">
        <h2>Skill-weighted signals</h2>
        <p className="lede">Live ideas combined per ticker. Each contributor's vote is weighted by <span className="mono">2·P(skill&gt;0) − 1</span>,
          so unproven contributors count for nothing. Generated {when(data.generated_at)}.</p>
        {live.length === 0 ? <p className="muted">No live ideas from contributors with demonstrated skill yet.</p> : (
          <table className="tbl">
            <thead><tr><th>ticker</th><th style={{ width: "40%" }}>net direction</th><th className="r">score</th>
              <th className="r">conviction</th><th className="r">long / short</th><th className="r">weight</th></tr></thead>
            <tbody>
              {live.map((s) => (
                <tr key={s.ticker}>
                  <td className="mono">{s.ticker}</td>
                  <td><div className="meter"><div className="meter-zero" />
                    <div className={`meter-fill ${s.score >= 0 ? "pos" : "neg"}`}
                         style={s.score >= 0 ? { left: "50%", width: `${s.score * 50}%` } : { left: `${50 + s.score * 50}%`, width: `${-s.score * 50}%` }} />
                  </div></td>
                  <td className="num">{num(s.score)}</td>
                  <td className="num">{num(s.weight_mass)}</td>
                  <td className="num">{s.n_long} / {s.n_short}</td>
                  <td className="num">{signedPct(data.portfolio[s.ticker] ?? 0, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="stack">
        <div className="card">
          <h3>Risk of the signal portfolio</h3>
          {data.risk ? (
            <dl className="stats">
              <div><dt>1-day VaR {pct(data.risk.level, 0)}</dt><dd>{pct(data.risk.var_1d, 2)}</dd></div>
              <div><dt>1-day CVaR (expected shortfall)</dt><dd>{pct(data.risk.cvar_1d, 2)}</dd></div>
              <div><dt>Annualised volatility</dt><dd>{pct(data.risk.vol_annualised, 1)}</dd></div>
              <div><dt>History replayed</dt><dd>{data.risk.days} days</dd></div>
            </dl>
          ) : <p className="muted">Needs at least one weighted position and 30 days of price history.</p>}
          <p className="small muted">Historical simulation: the last ~250 days of real returns run through today's weights. No normality assumption.</p>
        </div>
        {data.model && (
          <div className="card">
            <h3>Skill model</h3>
            <dl className="stats">
              <div><dt>σ: noise of one idea</dt><dd>{pct(Math.sqrt(data.model.sigma2), 1)}</dd></div>
              <div><dt>τ: spread of true skill</dt><dd>{pct(Math.sqrt(data.model.tau2), 2)}</dd></div>
              <div><dt>Closed ideas</dt><dd>{data.model.n_closed_ideas}</dd></div>
              <div><dt>Contributors</dt><dd>{data.model.n_contributors}</dd></div>
            </dl>
            <p className="small muted">When σ is large relative to τ, each idea says little about skill, so estimates shrink harder.
              With n ideas the shrink factor is n·τ² / (n·τ² + σ²).</p>
          </div>
        )}
      </div>
    </section>
  );
}
