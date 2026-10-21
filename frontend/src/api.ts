// Typed client for the Tally API.
export const API_BASE = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

const TOKEN_KEY = "tally.adminToken";
export const adminToken = {
  get: (): string => {
    try { return localStorage.getItem(TOKEN_KEY) ?? ""; } catch { return ""; }
  },
  set: (v: string) => {
    try { localStorage.setItem(TOKEN_KEY, v); } catch { /* storage unavailable */ }
  },
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, init: RequestInit & { admin?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (init.admin) headers["X-Admin-Token"] = adminToken.get();
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers: { ...headers, ...(init.headers as object) } });
  if (!res.ok) {
    let detail = res.statusText;
    try { const b = await res.json(); detail = typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail); } catch { /* not json */ }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export interface Mark {
  status: "pending_entry" | "open" | "closed" | "unscorable";
  entry_date: string | null; entry_price: number | null; bench_ticker: string | null;
  mark_date: string | null; mark_price: number | null;
  raw_return: number | null; bench_return: number | null; excess_return: number | null;
  normalised_excess: number | null; unscorable_reason: string | null;
}

export interface Idea {
  id: number; post_id: number; contributor_id: number; contributor: string | null;
  ticker: string; direction: "LONG" | "SHORT"; horizon_days: number; horizon_inferred: boolean;
  target_price: number | null; conviction: string | null; confidence: number; rationale: string | null;
  received_at: string; effective_at: string; extractor: string; prompt_version: string | null;
  mark: Mark | null;
}

export interface Candidate {
  ticker: string | null; direction: string; horizon_days: number; horizon_inferred: boolean;
  confidence: number; rationale: string; company_name: string | null; target_price: number | null;
}

export interface Post {
  id: number; source: string; contributor: string | null; text: string; url: string | null;
  posted_at: string | null; received_at: string; status: string; error: string | null;
  extractor: string | null; processed_at: string | null;
  extraction: { status: string; candidates: Candidate[]; notes: string[]; cached?: boolean } | null;
  ideas?: Idea[];
}

export interface IngestResult { post_id: number; status: string; duplicate: boolean; job_id: string | null }

export interface LeaderRow {
  contributor_id: number; handle: string; source: string; is_simulated: boolean;
  true_accuracy: number | null; n_ideas: number; n_closed: number; n_open: number; n_unscorable: number;
  hit_rate: number | null; mean_excess: number | null; posterior_mean: number | null;
  posterior_sd: number | null; ci95: [number, number] | null; prob_positive: number | null;
}

export interface Signal {
  ticker: string; score: number; weight_mass: number; n_ideas: number; n_long: number;
  n_short: number; n_contributors: number;
}

export interface SignalsPayload {
  generated_at: string | null; signals: Signal[]; portfolio: Record<string, number>;
  risk: { level: number; days: number; var_1d: number; cvar_1d: number; vol_annualised: number } | null;
  model?: { sigma2: number; tau2: number; n_contributors: number; n_closed_ideas: number };
}

export interface SystemInfo {
  queue: { backend: string; waiting?: number; in_progress?: number; dead?: number; oldest_waiting_s?: number | null; error?: string };
  dead_letters: { id: string; payload: { post_id?: number }; attempts: number; error: string | null; at: string | null }[];
  workers: { worker_id: string; processed: number; failed: number; last_seen: string; alive: boolean }[];
  scoring_runs: { id: number; started_at: string; finished_at: string | null; ideas_marked: number; contributors_scored: number; error: string | null }[];
  post_counts: Record<string, number>;
  simulated_seeded: boolean;
  config: {
    extractor: string; llm_requests_per_minute: number | null; review_confidence_threshold: number;
    market_data: string; scoring_interval_minutes: number | null; queue_backend: string;
    lease_seconds: number; max_attempts: number; sources: string[];
  };
}
