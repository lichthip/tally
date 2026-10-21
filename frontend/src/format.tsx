// =============================================================================
// format.tsx: SMALL DISPLAY HELPERS SHARED BY ALL PAGES
// -----------------------------------------------------------------------------
// Number/date formatters (missing values show as "—"), and three tiny components:
//   <Ret value={0.034} />  -> "+3.40%" in green (or red if negative)
//   <Badge kind="ok">...</Badge> -> a coloured pill
//   <Dir d="LONG" />       -> LONG/SHORT label, styled by direction
// =============================================================================
import type { ReactNode } from "react";    // ReactNode = anything React can display

// 0.034 -> "3.4%"
export const pct = (x: number | null | undefined, digits = 1): string =>
  x === null || x === undefined ? "—" : `${(x * 100).toFixed(digits)}%`;

// 0.034 -> "+3.40%", -0.01 -> "-1.00%" (an explicit + sign for gains)
export const signedPct = (x: number | null | undefined, digits = 2): string =>
  x === null || x === undefined ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;

export const num = (x: number | null | undefined, digits = 2): string =>
  x === null || x === undefined ? "—" : x.toFixed(digits);

// ISO timestamp -> a short local date/time in the VIEWER's timezone ("21 Oct 26, 16:05").
export const when = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { year: "2-digit", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

// "2026-10-21T08:00:00Z" -> "2026-10-21"
export const day = (iso: string | null | undefined): string => (iso ? iso.slice(0, 10) : "—");

// A return value, coloured by sign via the CSS classes "pos" / "neg" / "muted".
// ({ value, digits = 2 }: {...}) = take the props object apart into variables ("destructuring").
export function Ret({ value, digits = 2 }: { value: number | null | undefined; digits?: number }): ReactNode {
  const cls = value === null || value === undefined ? "muted" : value > 0 ? "pos" : value < 0 ? "neg" : "";
  return <span className={`num ${cls}`}>{signedPct(value, digits)}</span>;
}

// `children` = whatever is written between <Badge> and </Badge>.
export function Badge({ kind, children }: { kind: string; children: ReactNode }) {
  return <span className={`badge badge-${kind}`}>{children}</span>;
}

export function Dir({ d }: { d: string }) {
  return <span className={`dir dir-${d.toLowerCase()}`}>{d}</span>;
}
