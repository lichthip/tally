import type { ReactNode } from "react";

export const pct = (x: number | null | undefined, digits = 1): string =>
  x === null || x === undefined ? "—" : `${(x * 100).toFixed(digits)}%`;

export const signedPct = (x: number | null | undefined, digits = 2): string =>
  x === null || x === undefined ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;

export const num = (x: number | null | undefined, digits = 2): string =>
  x === null || x === undefined ? "—" : x.toFixed(digits);

export const when = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { year: "2-digit", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

export const day = (iso: string | null | undefined): string => (iso ? iso.slice(0, 10) : "—");

export function Ret({ value, digits = 2 }: { value: number | null | undefined; digits?: number }): ReactNode {
  const cls = value === null || value === undefined ? "muted" : value > 0 ? "pos" : value < 0 ? "neg" : "";
  return <span className={`num ${cls}`}>{signedPct(value, digits)}</span>;
}

export function Badge({ kind, children }: { kind: string; children: ReactNode }) {
  return <span className={`badge badge-${kind}`}>{children}</span>;
}

export function Dir({ d }: { d: string }) {
  return <span className={`dir dir-${d.toLowerCase()}`}>{d}</span>;
}
