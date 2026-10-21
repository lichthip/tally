"""Extraction eval: how often does the extractor get it right, and where does it fail?

A hand-labelled set of posts (tally/data/eval_posts.jsonl) covers the known hard cases:
options (buying puts = SHORT, selling puts = LONG), sarcasm, questions, conditionals,
hearsay, pair trades, company names, SGX tickers, prompt injection...

Metrics:
  * is-idea precision / recall: does it correctly tell ideas from noise?
  * ticker accuracy and direction accuracy on true ideas
  * fully-correct rate: everything right for the post (the number that matters)
  * per-tag breakdown, so a prompt change can be judged by WHICH cases it fixes/breaks
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import time

from ..extraction.llm_clients import LLMError, LLMPermanentError, LLMTransientError
from ..extraction.symbols import normalise_ticker
from ..extraction.types import RawExtraction

DATA = Path(__file__).resolve().parent.parent / "data" / "eval_posts.jsonl"
POSTED_AT = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)


def load_cases(path: Path = DATA) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _accepts(expected_ticker, got: str | None) -> bool:
    options = expected_ticker if isinstance(expected_ticker, list) else [expected_ticker]
    return got in options


def predict(extractor, resolver, text: str) -> tuple[bool, list[tuple[str | None, str]], str | None]:
    """Returns (is_idea, [(ticker, direction)], error)."""
    try:
        raw = _extract_with_retry(extractor, text)
        raw.pop("_cached", None)
        parsed = RawExtraction.model_validate(raw)
    except LLMPermanentError:
        raise   # bad key / unknown model: every case would fail the same way, so stop now
    except (LLMError, ValueError) as e:
        return False, [], f"{type(e).__name__}: {e}"
    ideas = []
    for i in parsed.ideas:
        t, _ = resolver.resolve(i.ticker, i.company_name)
        ideas.append((t or normalise_ticker(i.ticker), i.direction))
    return parsed.is_trade_idea and bool(ideas), ideas, None


def _extract_with_retry(extractor, text: str, attempts: int = 3) -> dict:
    """Provider hiccups (500s, timeouts) say nothing about extraction quality, so retry
    them, the same way the production queue would, instead of scoring them as wrong."""
    for i in range(attempts):
        try:
            return extractor.extract(text, POSTED_AT)
        except LLMTransientError:
            if i == attempts - 1:
                raise
            time.sleep(2 ** (i + 1))
    raise AssertionError("unreachable")


def score_case(case: dict, is_idea: bool, ideas: list[tuple[str | None, str]]) -> dict:
    exp = case["ideas"]
    res = {"idea_correct": is_idea == case["is_idea"], "tickers_correct": None, "directions_correct": None}
    if case["is_idea"] and is_idea:
        matched, dir_ok = 0, 0
        for e in exp:
            hit = next((d for t, d in ideas if _accepts(e["ticker"], t)), None)
            if hit is not None:
                matched += 1
                dir_ok += hit == e["direction"]
        res["tickers_correct"] = matched == len(exp) and len(ideas) == len(exp)
        res["directions_correct"] = dir_ok == len(exp)
    res["fully_correct"] = res["idea_correct"] and (not case["is_idea"] or
                                                    (res["tickers_correct"] and res["directions_correct"]))
    return res


def run_eval(extractor, resolver, limiter=None, limit: int | None = None) -> dict:
    cases = load_cases()[:limit] if limit else load_cases()
    tp = fp = fn = tn = 0
    tick_ok = tick_n = dir_ok = 0
    full = 0
    infra_errors = 0
    by_tag: dict[str, list[bool]] = defaultdict(list)
    failures = []
    for case in cases:
        if limiter is not None:
            limiter.acquire()
        is_idea, ideas, err = predict(extractor, resolver, case["text"])
        if err and "Transient" in err:
            infra_errors += 1
        s = score_case(case, is_idea, ideas)
        tp += case["is_idea"] and is_idea
        fp += (not case["is_idea"]) and is_idea
        fn += case["is_idea"] and not is_idea
        tn += (not case["is_idea"]) and not is_idea
        if s["tickers_correct"] is not None:
            tick_n += 1
            tick_ok += s["tickers_correct"]
            dir_ok += s["directions_correct"]
        full += s["fully_correct"]
        for t in case["tags"]:
            by_tag[t].append(s["fully_correct"])
        if not s["fully_correct"]:
            failures.append({"id": case["id"], "text": case["text"], "tags": case["tags"],
                             "expected": case["ideas"] if case["is_idea"] else "no idea",
                             "got": [{"ticker": t, "direction": d} for t, d in ideas] if is_idea else "no idea",
                             "error": err})
    n = len(cases)
    return {
        "extractor": extractor.id, "n": n,
        "idea_precision": tp / (tp + fp) if tp + fp else None,
        "idea_recall": tp / (tp + fn) if tp + fn else None,
        "idea_accuracy": (tp + tn) / n if n else None,
        "ticker_accuracy": tick_ok / tick_n if tick_n else None,
        "direction_accuracy": dir_ok / tick_n if tick_n else None,
        "fully_correct": full / n if n else None,
        "infra_errors": infra_errors,
        "by_tag": {t: {"n": len(v), "correct": sum(v)} for t, v in sorted(by_tag.items())},
        "failures": failures,
    }


def to_markdown(r: dict) -> str:
    pct = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
    out = [f"## Extractor `{r['extractor']}` ({r['n']} labelled posts)\n",
           "| Metric | Value |", "|---|---|",
           f"| Fully correct | **{pct(r['fully_correct'])}** |",
           f"| Is-idea precision | {pct(r['idea_precision'])} |",
           f"| Is-idea recall | {pct(r['idea_recall'])} |",
           f"| Ticker accuracy (true ideas found) | {pct(r['ticker_accuracy'])} |",
           f"| Direction accuracy (true ideas found) | {pct(r['direction_accuracy'])} |",
           f"| Provider errors after retries (counted as wrong) | {r.get('infra_errors', 0)} |",
           "", "| Tag | Correct |", "|---|---|"]
    for t, v in r["by_tag"].items():
        out.append(f"| {t} | {v['correct']}/{v['n']} |")
    out += ["", "### Failures", ""]
    for f in r["failures"]:
        out.append(f"- **#{f['id']}** ({', '.join(f['tags'])}): \"{f['text']}\"  \n"
                   f"  expected `{json.dumps(f['expected'])}`, got `{json.dumps(f['got'])}`"
                   + (f" (error: {f['error']})" if f["error"] else ""))
    return "\n".join(out) + "\n"


def run_eval_cli(rt, args) -> int:
    from ..extraction.symbols import SymbolResolver
    resolver = SymbolResolver(rt.sf)   # no network lookups: the eval must not mutate the symbol master
    try:
        r = run_eval(rt.extractor, resolver, rt.limiter, args.limit)
    except LLMPermanentError as e:
        print(f"\nLLM configuration error, eval aborted:\n  {e}\n"
              "Run `python -m tally check-llm` to see which models your key can use.")
        return 2
    md = to_markdown(r)
    print(md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("# Extraction eval\n\n" + md)
    return 0
