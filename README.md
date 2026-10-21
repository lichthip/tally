# Tally

**Who actually makes good calls?** Tally ingests trade ideas from wherever people post them (Reddit, newsletters, a manual API), uses an LLM to turn free text into structured ideas, keeps an **append-only, timestamped record**, marks every idea to market against a benchmark, and estimates each contributor's skill with **empirical-Bayes shrinkage**, so a lucky 3-for-3 streak doesn't top the leaderboard.

It's a personal-scale take on alpha capture: collect many people's ideas, measure them honestly, and weight future ideas by demonstrated skill.

```
post ──► API (dedupe, store, enqueue) ──► queue ──► worker (LLM extract → validate) ──► append-only idea log
                                                                                              │
         leaderboard ◄── skill model ◄── scoring job (mark to market vs benchmark, daily) ◄───┘
         signals + VaR ◄─┘
```

## What's interesting in here

| Problem | How Tally handles it | Where |
|---|---|---|
| Two workers grabbing the same job | `SELECT … FOR UPDATE SKIP LOCKED` claim in one statement | `queue/postgres_queue.py` |
| A worker crashes mid-job | Leases with expiry instead of held locks; fencing tokens reject stale acks | `queue/*` |
| Retries cause duplicate work | At-least-once delivery + idempotent handler (row lock + `UNIQUE(post_id, ticker)`) | `worker.py` |
| Saving the post and enqueuing are two systems | Postgres: same transaction. Redis: dual write + sweeper repair loop | `ingestion/service.py` |
| Postgres vs Redis as the queue | One `JobQueue` interface, two backends, **benchmarked** | `docs/benchmarks.md` |
| LLM output is untrusted | Pydantic schema validation, symbol master check, confidence gate, human review | `extraction/pipeline.py` |
| Free LLM tiers allow ~10 req/min | Token-bucket limiter acquired *before* claiming a job; response cache | `extraction/llm_clients.py` |
| Rewriting history | DB trigger makes `trade_ideas` append-only, so even raw SQL can't edit it | `db.py` |
| Lookahead bias | Entry = next tradable **open** after receipt, in the stock's exchange timezone | `scoring/marking.py` |
| Bull markets make everyone look smart | Excess return vs benchmark (SPY / STI ETF / Tracker Fund) | `scoring/marking.py` |
| Luck vs skill | Empirical-Bayes shrinkage, validated on simulated contributors with known skill | `scoring/skill.py`, `tools/simulate.py` |
| Is the LLM actually right? | 48 hand-labelled hard cases, per-tag accuracy, rule-based baseline | `docs/eval.md` |

More: [Architecture](docs/ARCHITECTURE.md) · [Design decisions & trade-offs](docs/DESIGN_DECISIONS.md) · [Benchmarks](docs/benchmarks.md) · [Extraction eval](docs/eval.md) · [Deployment](docs/DEPLOYMENT.md)

## Stack

Python 3.10+ (3.12 recommended), FastAPI, SQLAlchemy 2, Postgres, Redis Streams (optional), httpx, yfinance · React + TypeScript + Vite · pytest.

## Free LLM options

Extraction works with any of these (set `LLM_PROVIDER` in `backend/.env`). All have free tiers, but limits change often, so check the provider's console:

| Provider | Get a key | Default model in Tally | Notes |
|---|---|---|---|
| **Gemini** (Google AI Studio) | aistudio.google.com/apikey | `gemini-flash-latest` | Supports schema-constrained JSON output. On the free tier, Google may use prompts to improve its products. |
| **Groq** | console.groq.com/keys | `openai/gpt-oss-120b` | Very fast; OpenAI-compatible. Groq retires models often, so run `python -m tally check-llm` to see what your key can use. |
| **OpenRouter** | openrouter.ai/keys | `meta-llama/llama-3.3-70b-instruct:free` | Many `:free` models; low daily cap without credits. |
| `rules` | none | regex baseline | Offline. Good for tests and for showing why an LLM is needed. |

## Run it locally (Windows / macOS / Linux)

**1. Postgres.** Either install Postgres locally and create a database called `tally`, or create a free database at [neon.tech](https://neon.tech) and copy its connection string.

**2. Backend**

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt
copy .env.example .env            # Windows   (macOS/Linux: cp .env.example .env)
# edit .env: DATABASE_URL, LLM_PROVIDER + API key, ADMIN_TOKEN
python -m tally init-db
python -m tally serve             # API on http://127.0.0.1:8000  (docs at /docs)
```

With `EMBEDDED_WORKER=true` (the default in `.env.example`) the API process also runs the extraction worker and the scoring scheduler. To run them as separate processes instead (closer to a real deployment), set both embedded flags to `false` and use three terminals:

```bash
python -m tally serve
python -m tally worker --concurrency 2
python -m tally scheduler
```

**3. Seed some history.** This creates simulated contributors with known skill, scores them, and prints how well the scorer recovered the truth:

```bash
python -m tally simulate
```

**4. Frontend**

```bash
cd frontend
npm install
copy .env.example .env            # VITE_API_URL=http://localhost:8000
npm run dev                       # http://localhost:5173
```

## Useful commands

```bash
python -m tally submit "Long $NVDA into earnings" --author me   # ingest one post
python -m tally score                                           # one scoring pass
python -m tally eval --out ../docs/eval_llm.md                      # extraction eval with your LLM
python -m tally bench --backend both --crash-rate 0.05          # queue benchmark
python -m tally poll                                            # poll configured Reddit/RSS sources
pytest -q                                                       # tests (needs a tally_test database)
```

Tests expect a Postgres database named `tally_test` (override with `TEST_DATABASE_URL`). Redis tests fall back to `fakeredis` if no Redis is running.

## Project layout

```
backend/tally/
  api/            FastAPI app, routes, auth + rate-limit dependencies
  queue/          JobQueue interface, PostgresQueue (SKIP LOCKED), RedisStreamQueue
  ingestion/      idempotent ingestion + dual-write sweeper
  sources/        one class per file: Source (abstract) -> RSSSource -> RedditRSSSource / SubstackRSSSource,
                  StockTwitsAPISource, RedditAPISource (blocked, kept); factory.py registry
  extraction/     prompt, LLM clients, extractors (strategy pattern), validation pipeline, symbols
  market/         price providers (yfinance / synthetic), Postgres read-through cache, exchanges
  scoring/        marking (pure), skill model (pure), signals + VaR (pure), scoring job
  tools/          simulator, eval harness, queue benchmark
  worker.py       claim → extract → persist → ack
  review.py       human-in-the-loop approval
  runtime.py      composition root (dependency injection)
  models.py       schema; db.py adds the append-only trigger
backend/tests/    unit + integration tests (74)
frontend/src/     React UI: submit, leaderboard, ideas, signals, review, system
docs/             architecture, decisions, benchmarks, eval, deployment
deploy/github-workflows/   CI (tests + build) and daily scoring; copy into .github/workflows/ when you push to GitHub
render.yaml       Render blueprint for the API
```
