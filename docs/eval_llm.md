# Extraction eval

## Extractor `groq:openai/gpt-oss-120b` (48 labelled posts)

| Metric | Value |
|---|---|
| Fully correct | **96%** |
| Is-idea precision | 100% |
| Is-idea recall | 97% |
| Ticker accuracy (true ideas found) | 97% |
| Direction accuracy (true ideas found) | 100% |

| Tag | Correct |
|---|---|
| ambiguous_listing | 1/1 |
| basic | 5/6 |
| cashtag | 2/2 |
| commentary | 1/1 |
| company_name | 5/6 |
| conditional | 1/1 |
| consumer_not_investment | 1/1 |
| etf | 2/2 |
| exit_not_short | 1/1 |
| exit_with_view | 1/1 |
| hearsay | 2/2 |
| horizon | 2/3 |
| multi | 1/1 |
| negation | 1/1 |
| neutral_strategy | 2/2 |
| no_security | 2/2 |
| not_in_symbol_master | 1/1 |
| options | 3/3 |
| pair_trade | 1/1 |
| past_trade | 1/1 |
| prompt_injection | 1/1 |
| question | 4/4 |
| relative_view | 1/1 |
| sarcasm | 2/2 |
| sgx | 3/3 |
| slang | 3/3 |
| target_date | 1/1 |
| ticker_format | 1/1 |
| tricky_direction | 2/2 |

### Failures

- **#15** (company_name, horizon): "Apple is massively undervalued here, adding for the next 6 months"  
  expected `[{"ticker": "AAPL", "direction": "LONG"}]`, got `"no idea"` (error: LLMTransientError: groq 500: {"error":{"message":"Internal Server Error","type":"internal_server_error"}}
)
- **#46** (basic): "Shorting MSTR and buying bitcoin directly instead"  
  expected `[{"ticker": "MSTR", "direction": "SHORT"}]`, got `[{"ticker": "MSTR", "direction": "SHORT"}, {"ticker": "BTC-USD", "direction": "LONG"}]`
