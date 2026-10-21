# Extraction eval

`backend/tally/data/eval_posts.jsonl` holds 48 hand-labelled posts covering the cases that break naive extraction: buying vs selling options, sarcasm, questions, conditionals, hearsay, past trades, pair trades, company names, SGX/HK tickers, ticker formats (BRK.B → BRK-B), and a prompt-injection attempt.

Run it against whichever extractor `.env` configures:

```bash
python -m tally eval --out ../docs/eval_llm.md      # LLM_PROVIDER=gemini / groq / openrouter / rules
```

**Caveat.** The labels, the prompt and the regex baseline were all written by the same person at the same time, so these numbers are optimistic for both. The honest next step is a held-out set labelled from real posts the system has never seen.

## Headline: LLM vs regex baseline

| Metric | Rule-based baseline | Groq `openai/gpt-oss-120b` |
|---|---|---|
| **Fully correct** | 75% | **96%** (47/48 after one provider retry) |
| Is-idea precision | 86% | 100% |
| Is-idea recall | 77% | 97% |
| Ticker accuracy | 100% | 97% |
| Direction accuracy | 96% | 100% |

The LLM fixes exactly the cases the baseline can't handle: sarcasm (2/2 vs 1/2), negation, relative views ("underperform the S&P"), names missing from the alias list (Carvana), and "bought a Tesla Model Y" (a product, not a stock). Full per-tag output is in [eval_llm.md](eval_llm.md).

**The two misses in the first run, and what they taught me**

1. **#15: a Groq `500 Internal Server Error`.** A provider outage, not a wrong answer. The production worker already retries these through the queue, but the eval scored it as a failure. Fix: the eval now retries transient errors with backoff and reports any remaining ones separately, so infrastructure noise isn't mixed into model quality.
2. **#46: "Shorting MSTR and buying bitcoin directly instead".** The label expected only MSTR SHORT; the model also returned `BTC-USD` LONG. That's arguably *correct*: the author is long bitcoin. I left the label unchanged on purpose, because editing labels after seeing model output is how evals quietly overfit. It's an ambiguity to resolve before the next labelling round. It also exposes a real gap: crypto trades 24/7, but the entry-price rule assumes exchange hours and an equity benchmark.

## Baseline: rule-based extractor (no LLM)

The regex/keyword extractor is the floor an LLM has to clear. It does well on explicit posts ("Long $NVDA") and fails where meaning depends on context: sarcasm, negation ("not touching PLTR, it'll correct" is bearish), relative views ("underperform the S&P"), names missing from the alias list (Carvana), and buying a product vs buying a stock ("bought a Tesla Model Y").

### Extractor `rules:v1` (48 labelled posts)

| Metric | Value |
|---|---|
| Fully correct | **75%** |
| Is-idea precision | 86% |
| Is-idea recall | 77% |
| Ticker accuracy (true ideas found) | 100% |
| Direction accuracy (true ideas found) | 96% |

| Tag | Correct |
|---|---|
| ambiguous_listing | 0/1 |
| basic | 6/6 |
| cashtag | 2/2 |
| commentary | 1/1 |
| company_name | 3/6 |
| conditional | 1/1 |
| consumer_not_investment | 0/1 |
| etf | 2/2 |
| exit_not_short | 1/1 |
| exit_with_view | 0/1 |
| hearsay | 1/2 |
| horizon | 2/3 |
| multi | 1/1 |
| negation | 0/1 |
| neutral_strategy | 1/2 |
| no_security | 2/2 |
| not_in_symbol_master | 0/1 |
| options | 2/3 |
| pair_trade | 1/1 |
| past_trade | 1/1 |
| prompt_injection | 1/1 |
| question | 4/4 |
| relative_view | 0/1 |
| sarcasm | 1/2 |
| sgx | 3/3 |
| slang | 3/3 |
| target_date | 0/1 |
| ticker_format | 1/1 |
| tricky_direction | 1/2 |

### Failures

- **#4** (options, tricky_direction): "Selling cash-secured puts on AAPL at 180, happy to own it there"  
  expected `[{"ticker": "AAPL", "direction": "LONG"}]`, got `[{"ticker": "AAPL", "direction": "SHORT"}]`
- **#9** (sarcasm): "Yeah GameStop to the moon again, what could possibly go wrong lol"  
  expected `"no idea"`, got `[{"ticker": "GME", "direction": "LONG"}]`
- **#11** (hearsay): "My uncle keeps telling me to buy Ford"  
  expected `"no idea"`, got `[{"ticker": "F", "direction": "LONG"}]`
- **#16** (company_name, not_in_symbol_master): "Shorting Carvana, the debt load is insane"  
  expected `[{"ticker": "CVNA", "direction": "SHORT"}]`, got `"no idea"`
- **#19** (neutral_strategy): "Writing covered calls on my KO shares again this month"  
  expected `"no idea"`, got `[{"ticker": "KO", "direction": "LONG"}]`
- **#25** (company_name): "I think Uber goes higher from here, ride volumes are strong"  
  expected `[{"ticker": "UBER", "direction": "LONG"}]`, got `"no idea"`
- **#26** (negation): "Not touching $PLTR at 100x sales, it'll correct hard"  
  expected `[{"ticker": "PLTR", "direction": "SHORT"}]`, got `"no idea"`
- **#29** (consumer_not_investment): "Just bought a Tesla Model Y, honestly love it"  
  expected `"no idea"`, got `[{"ticker": "TSLA", "direction": "LONG"}]`
- **#31** (ambiguous_listing): "Tencent is my top pick for 2027"  
  expected `[{"ticker": ["0700.HK", "TCEHY"], "direction": "LONG"}]`, got `"no idea"`
- **#33** (exit_with_view): "Took profits on all my GOOGL, I think it drops 20% from here"  
  expected `[{"ticker": "GOOGL", "direction": "SHORT"}]`, got `"no idea"`
- **#47** (relative_view, horizon): "Netflix will underperform the S&P over the next year"  
  expected `[{"ticker": "NFLX", "direction": "SHORT"}]`, got `"no idea"`
- **#48** (target_date, company_name): "Broadcom to 2000 by December"  
  expected `[{"ticker": "AVGO", "direction": "LONG"}]`, got `"no idea"`
