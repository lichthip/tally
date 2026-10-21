# Demo log: real runs and what they showed

A record of real results from building and testing Tally, kept for the presentation. Everything below comes from actual runs, not mock-ups. All data lives in the Neon Postgres database, so it's still there on demo day.

> **Before the demo:** in the Neon console, go to **Branches → Create branch** from `main` (e.g. `demo-snapshot-<date>`). A branch is a free, instant, copy-on-write snapshot of the whole database. If something goes wrong live, point `DATABASE_URL` at the snapshot branch and you're back to a known state.

---

## 1. LLM extraction quality: 96% vs 75% (eval)

`python -m tally eval --out ../docs/eval_llm.md`, 48 hand-labelled hard posts:

| | Keyword baseline | Groq `openai/gpt-oss-120b` |
|---|---|---|
| Fully correct | 75% | **96%** |
| Is-idea precision / recall | 86% / 77% | 100% / 97% |

The LLM gets every sarcasm, negation, options (buying vs selling puts), prompt-injection and "bought a Tesla car" case right. The two first-run misses are worth telling as stories (see `eval.md`):
- a Groq **500 error** was being scored as a wrong answer, so the eval now retries provider errors and reports them separately;
- "Shorting MSTR and buying bitcoin" returned an extra `BTC-USD LONG`. That's arguably correct, and the label was left unchanged on purpose, because editing labels after seeing model output is how evals quietly overfit.

## 2. A model retirement in production

The first Groq run failed on every call: `model 'llama-3.3-70b-versatile' does not exist or you do not have access`. Groq had retired the model. Lessons that turned into code:
- `python -m tally check-llm` lists the models a key can actually use and runs one test extraction;
- configuration errors (bad key, unknown model) now **abort** the eval with a clear message instead of producing a fake "20% accuracy";
- the model is a config value (`GROQ_MODEL`), so swapping it needs no code change.

## 3. Reddit locked down its API, so RSS took over

`python -m tally poll --subreddit ValueInvesting --dry-run` returned **`403 Blocked`**: Reddit now blocks anonymous JSON API access, and new OAuth apps need manual approval.

Fix: Reddit still publishes **RSS feeds** for every listing (`/r/<sub>/new/.rss`). Because every source implements the same `Source` interface, switching was one adapter class. Ingestion, extraction and scoring didn't change.

Real dry run (`python -m tally poll --rss "https://www.reddit.com/r/ValueInvesting/new/.rss" --limit 10 --dry-run`), 20 Oct 2026:

```
[drop] u/HopeFlyingSquarrel   'Tip for Korean Stock(feat. SK hynix, Samsung). I guess due to low PER...'
[drop] u/Far-East-locker      'The real reason data center stock are crashing...'
[drop] u/Muted_Variation8830  "New to investing. Hi everyone!..."
[KEEP] u/StableBread          'Reading today's macro environment with 5 measures, and how I'd position my portfolio...'
[KEEP] u/the_axe_effect       'HOOD Seems super overvalued. Hi All, I did a deep dive into HOOD stock...'
[drop] u/GlennThomas_         "What makes you revisit an investment thesis?..."
[KEEP] u/StockCompil          'FICO in Q3 fund letters: 2 managers kept it, 3 sold it...'
[drop] u/ultra__star          'Tootsie Roll (TR) is a cash cow fortress run by an unbothered 94 year old...'
[drop] u/Boston-Bets          'Gartner analyst behind tech rout...'
[drop] u/Kind_Soup_5285       'LLMs and Valuation - Findings and Methodology...'
-> 10 fetched, 3 would be ingested (= up to 3 LLM calls)
```

**Talking point: the pre-filter trade-off.** The cheap keyword filter saved 7 of 10 LLM calls, but it wrongly dropped the Tootsie Roll **(TR)** pitch because `TR` isn't in the ~115-ticker symbol list. A pre-filter always trades some recall for cost.

## 4. What the LLM decided on real Reddit posts

Processed for real (`poll --rss ...` then `worker`). From Neon:

| Post | Verdict | LLM's reason | Correct? |
|---|---|---|---|
| #413 FICO fund letters | `no_idea` | "author reports fund managers' actions without expressing their own forward-looking view" | Yes: hearsay rule worked |
| #411 Macro positioning | `no_idea` | "macro commentary without a specific forward-looking view on a tradable security" | Yes |
| #412 "HOOD seems super overvalued" | `no_idea` | "no explicit forward-looking trade direction, just commentary" | **Arguably wrong**: reads as bearish |

## 5. Fixing the false "no idea": a second opinion, and human review

The HOOD case is the real-world false negative the eval's 97% recall predicts. Instead of loosening the prompt (which would mean re-running and re-justifying the whole eval), Tally now asks the **keyword baseline for a second opinion** whenever the LLM says "no idea". If the baseline sees a ticker plus a direction word, the two disagree, and the post goes to the **Review tab** instead of being silently dropped.

`python -m tally reprocess 412` re-ran it (using the cached LLM answer, so no extra API call). Review then showed `LLM: no idea …` plus `keyword baseline disagrees, found: HOOD SHORT`.

Testing this on real data also exposed two baseline bugs, both now fixed and tested:
- **"Robinhood Gold" was read as the GLD gold ETF**, and a passing "S&P" became SPY. Generic-word aliases (gold, oil, S&P, block, strategy…) are no longer keyword-matched; the LLM resolves them from context.
- **Direction came from the whole post's mood** ("amazing news… healthy business" made HOOD look LONG). It now uses the direction word nearest the ticker, on either side: "HOOD seems super overvalued" gives SHORT. Options right after a ticker still win ("buying TSLA puts" gives SHORT).

**What approve/reject do:**
- **Approve** checks each ticker against the symbol master and writes the ideas to the append-only log with `effective_at = approval time`. The next scoring run sets the entry at the first market open after approval, so a reviewer can't approve only the ideas that already went up. The post becomes `extracted`.
- **Reject** sets the post to `rejected`. No ideas are created, but the post stays in the database for audit and can be re-run with `reprocess`.

## 6. Engineering numbers (benchmarks.md)

Postgres queue vs Redis Streams, same workload: Redis enqueues about 9x faster, with about 3 ms vs about 120 ms p95 latency. With 5% of workers crashing after doing the work but before acking, **every job still completed, and every crashed job ran twice**. That's at-least-once delivery, and it's why the worker is idempotent. Postgres is still the default because the job is saved in the same transaction as the post, and the LLM's ~8 calls a minute is the real bottleneck.

## 7. Scoring validation (simulate)

`python -m tally simulate` plants contributors with known accuracy and checks the scorer recovers them. `sim_lucky_streak` (100% hit rate on 2–3 ideas) drops from the top of a raw ranking to the middle once its score is shrunk toward zero. The simulation also caught a real bug: the first skill estimator let tiny accounts drag the skill-spread estimate to zero, which flattened every score.

---

### Demo commands cheat sheet

```
python -m tally check-llm                                         # LLM key/model sanity check
python -m tally serve                                             # API + worker + scheduler
python -m tally poll --rss "https://www.reddit.com/r/ValueInvesting/new/.rss" --limit 10 --dry-run
python -m tally reprocess <post_id>                               # re-run extraction on a no-idea post
python -m tally score                                             # scoring pass now
python -m tally simulate --report-only                            # print the skill-recovery report
```
Frontend: `cd frontend && npm run dev`, then http://localhost:5173 (Submit, Leaderboard, Ideas, Signals, Review, System).
