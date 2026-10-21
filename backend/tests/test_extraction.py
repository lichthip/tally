"""Extraction pipeline: validation, symbol resolution, routing. No network, no DB."""
import datetime as dt

import pytest

from tally.extraction.extractors import IdeaExtractor, LLMIdeaExtractor, RuleBasedExtractor
from tally.extraction.llm_clients import LLMBadOutput, LLMClient, RateLimiter, parse_json_text
from tally.extraction.pipeline import ExtractionPipeline, calendar_to_trading_days
from tally.extraction.symbols import SymbolResolver, normalise_ticker

POSTED = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)
SYMBOLS = [("AAPL", "Apple Inc.", ["apple"]), ("NVDA", "NVIDIA Corporation", ["nvidia"]),
           ("TSLA", "Tesla Inc.", ["tesla"]), ("AMD", "Advanced Micro Devices", ["amd"]),
           ("INTC", "Intel Corporation", ["intel"]), ("D05.SI", "DBS Group", ["dbs"])]


class Fixed(IdeaExtractor):
    id = "fixed:test"

    def __init__(self, out):
        self.out = out

    def extract(self, text, posted_at):
        if isinstance(self.out, Exception):
            raise self.out
        return dict(self.out)


def idea(**kw):
    base = {"ticker": "NVDA", "company_name": None, "direction": "LONG", "horizon_days": None,
            "target_date": None, "target_price": None, "conviction": "high", "confidence": 0.9,
            "rationale": "r"}
    return {**base, **kw}


def run(out, **kw):
    resolver = SymbolResolver(None, symbols=SYMBOLS, exists_fn=kw.pop("exists_fn", None))
    return ExtractionPipeline(Fixed(out), resolver, **kw).run("text", POSTED)


def test_clean_idea_is_extracted_with_default_horizon():
    o = run({"is_trade_idea": True, "reason": "", "ideas": [idea()]})
    assert o.status == "extracted"
    assert o.ideas[0].ticker == "NVDA" and o.ideas[0].horizon_days == 20 and o.ideas[0].horizon_inferred


def test_target_date_becomes_trading_day_horizon():
    o = run({"is_trade_idea": True, "ideas": [idea(target_date="2026-12-31")]})
    assert o.ideas[0].horizon_days == calendar_to_trading_days(91) and not o.ideas[0].horizon_inferred


def test_company_name_resolves_via_alias():
    o = run({"is_trade_idea": True, "ideas": [idea(ticker=None, company_name="Apple")]})
    assert o.status == "extracted" and o.ideas[0].ticker == "AAPL"


def test_unknown_ticker_goes_to_review():
    o = run({"is_trade_idea": True, "ideas": [idea(ticker="ZZZZ")]})
    assert o.status == "needs_review" and o.ideas[0].ticker is None
    assert any("unresolved" in n for n in o.notes)


def test_unknown_ticker_with_price_history_is_accepted():
    o = run({"is_trade_idea": True, "ideas": [idea(ticker="CVNA")]}, exists_fn=lambda t: t == "CVNA")
    assert o.status == "extracted" and o.ideas[0].ticker == "CVNA"


def test_low_confidence_goes_to_review():
    o = run({"is_trade_idea": True, "ideas": [idea(confidence=0.3)]})
    assert o.status == "needs_review"


def test_not_an_idea():
    assert run({"is_trade_idea": False, "reason": "question", "ideas": []}).status == "no_idea"


def test_contradictory_output_goes_to_review():
    assert run({"is_trade_idea": False, "ideas": [idea()]}).status == "needs_review"


def test_schema_violation_goes_to_review():
    o = run({"is_trade_idea": True, "ideas": [idea(direction="SIDEWAYS")]})
    assert o.status == "needs_review" and "schema" in o.notes[0]


def test_direction_synonyms_are_normalised():
    o = run({"is_trade_idea": True, "ideas": [idea(direction="bearish")]})
    assert o.ideas[0].direction == "SHORT"


def test_duplicate_tickers_merged():
    o = run({"is_trade_idea": True, "ideas": [idea(), idea(ticker="$nvda")]})
    assert len(o.ideas) == 1


def test_bad_model_output_goes_to_review_not_retry():
    o = run(LLMBadOutput("not json"))
    assert o.status == "needs_review"


@pytest.mark.parametrize("raw,expected", [("$brk.b", "BRK-B"), ("D05.SI", "D05.SI"),
                                          ("NASDAQ:aapl", "AAPL"), (None, None)])
def test_normalise_ticker(raw, expected):
    assert normalise_ticker(raw) == expected


def test_parse_json_tolerates_code_fences():
    assert parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMBadOutput):
        parse_json_text("sure! here you go")


class TestRuleBaseline:
    ex = RuleBasedExtractor([(t, a) for t, _, a in SYMBOLS])

    def ideas(self, text):
        out = self.ex.extract(text, POSTED)
        return [(i["ticker"], i["direction"]) for i in out["ideas"]] if out["is_trade_idea"] else []

    def test_buying_puts_is_short(self):
        assert self.ideas("Grabbed some $TSLA puts") == [("TSLA", "SHORT")]

    def test_selling_puts_is_long(self):
        assert self.ideas("Selling puts on AAPL, happy to own it") == [("AAPL", "LONG")]

    def test_question_is_not_an_idea(self):
        assert self.ideas("Should I buy NVDA?") == []

    def test_pair_trade(self):
        assert self.ideas("Long AMD, short INTC") == [("AMD", "LONG"), ("INTC", "SHORT")]

    def test_direction_word_after_ticker(self):
        assert self.ideas("TSLA seems super overvalued, did a deep dive. Great product though, love it") \
            == [("TSLA", "SHORT")]

    def test_generic_alias_is_not_a_ticker(self):
        ex = RuleBasedExtractor([("GLD", ["gold"]), ("TSLA", ["tesla"])])
        out = ex.extract("Tesla is overvalued. Robinhood Gold members love it", POSTED)
        assert [i["ticker"] for i in out["ideas"]] == ["TSLA"]

    def test_backward_looking_duration_is_not_a_horizon(self):
        out = self.ex.extract("Short $TSLA, revenue CAGR over the last 3 years is slowing", POSTED)
        assert out["ideas"][0]["horizon_days"] is None
        out = self.ex.extract("Long $NVDA for the next 6 months", POSTED)
        assert out["ideas"][0]["horizon_days"] == 180

    def test_horizon_and_month(self):
        out = self.ex.extract("Buying $NVDA by March", POSTED)
        assert out["ideas"][0]["target_date"] == "2027-03-31"


class FakeClient(LLMClient):
    provider, model = "fake", "m1"

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, gemini_schema=None):
        self.calls += 1
        assert "<<<POST" in user and "untrusted" in system   # prompt hygiene
        return {"is_trade_idea": True, "reason": "", "ideas": [idea()]}


class DictCache:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def put(self, k, model, pv, resp):
        self.d[k] = resp


def test_llm_extractor_caches_identical_requests():
    client = FakeClient()
    ex = LLMIdeaExtractor(client, DictCache())
    first = ex.extract("Long NVDA", POSTED)
    second = ex.extract("Long NVDA", POSTED)
    assert client.calls == 1
    assert "_cached" not in first and second["_cached"] is True
    ex.extract("Long NVDA", POSTED + dt.timedelta(days=40))   # different date -> different key
    assert client.calls == 2


def test_rate_limiter_bucket():
    rl = RateLimiter(rate_per_minute=60, burst=2)
    assert rl.acquire(timeout=0) and rl.acquire(timeout=0)
    assert not rl.acquire(timeout=0)       # bucket empty
    rl.refund()
    assert rl.acquire(timeout=0)


def test_second_opinion_sends_disputed_no_idea_to_review():
    resolver = SymbolResolver(None, symbols=SYMBOLS)
    baseline = RuleBasedExtractor([(t, a) for t, _, a in SYMBOLS])
    llm_says_no = Fixed({"is_trade_idea": False, "reason": "just commentary", "ideas": []})
    p = ExtractionPipeline(llm_says_no, resolver, second_opinion=baseline)
    o = p.run("TSLA seems super overvalued, bearish here", POSTED)
    assert o.status == "needs_review"
    assert [(c.ticker, c.direction) for c in o.ideas] == [("TSLA", "SHORT")]
    assert "disagrees" in o.notes[1]
    # when both agree there is no idea, it stays no_idea
    assert p.run("Markets feel frothy, be careful out there", POSTED).status == "no_idea"
