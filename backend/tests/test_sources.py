"""Source adapters against mocked HTTP, plus the preview/ingest API with an injected source."""
import httpx
import pytest
from fastapi.testclient import TestClient

from tally.schemas import PostIn
from tally.sources import (
    RedditAPISource,
    RedditRSSSource,
    RSSSource,
    Source,
    SourceError,
    StockTwitsAPISource,
    SubstackRSSSource,
    build_source,
)


def mock(src, content=b"", json=None, status=200):
    def handler(_req):
        return httpx.Response(status, json=json) if json is not None else httpx.Response(status, content=content)
    src._http = httpx.Client(transport=httpx.MockTransport(handler))
    return src


REDDIT_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>newest submissions : stocks</title>
<entry><author><name>/u/deepvalue99</name></author><id>t3_abc</id>
<link href="https://www.reddit.com/r/stocks/comments/abc/x/"/><published>2026-10-20T03:00:00+00:00</published>
<title>Buying more GOOGL</title><content type="html">&lt;p&gt;cheapest megacap&lt;/p&gt; submitted by &lt;a href="x"&gt;/u/deepvalue99&lt;/a&gt; &lt;span&gt;&lt;a href="y"&gt;[link]&lt;/a&gt;&lt;/span&gt; &lt;span&gt;&lt;a href="z"&gt;[comments]&lt;/a&gt;&lt;/span&gt;</content></entry></feed>"""


def test_reddit_is_an_rss_source_with_a_url_pattern():
    src = RedditRSSSource("r/stocks", limit=5)
    assert isinstance(src, RSSSource) and isinstance(src, Source)     # inheritance chain
    assert src.url == "https://www.reddit.com/r/stocks/new/.rss" and src.label == "reddit:r/stocks"
    [post] = list(mock(src, REDDIT_ATOM).fetch())
    assert (post.source, post.author, post.external_id) == ("reddit", "deepvalue99", "t3_abc")
    assert "GOOGL" in post.text and "cheapest megacap" in post.text
    assert "submitted by" not in post.text and "[comments]" not in post.text   # footer stripped


SUBSTACK_RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"
 xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Stocks Under Rocks</title><item><title>Ideas I find interesting</title>
<description>A short subtitle with no tickers</description>
<content:encoded><![CDATA[<p>This week I bought more <b>$AMZN</b>; AWS margins keep expanding.</p>]]></content:encoded>
<dc:creator>Stocks Under Rocks</dc:creator><guid>https://x.substack.com/p/ideas</guid>
<pubDate>Sun, 18 Oct 2026 16:22:01 GMT</pubDate></item></channel></rss>"""


def test_substack_is_an_rss_source_with_its_own_label():
    src = SubstackRSSSource("StocksUnderRocks")
    assert isinstance(src, RSSSource) and src.url == "https://stocksunderrocks.substack.com/feed"
    [post] = list(mock(src, SUBSTACK_RSS).fetch())
    assert post.source == "substack" and "$AMZN" in post.text and src.label == "substack:stocksunderrocks"
    assert SubstackRSSSource("https://www.netinterest.co").url == "https://www.netinterest.co/feed"


def test_reddit_api_source_surfaces_the_block():
    import httpx as _h
    src = mock(RedditAPISource("stocks"), content=b"Blocked", status=403)
    with pytest.raises(_h.HTTPStatusError):
        list(src.fetch())


def test_rss_uses_full_body_not_just_the_subtitle():
    [post] = list(mock(RSSSource("https://x.substack.com/feed"), SUBSTACK_RSS).fetch())
    assert "$AMZN" in post.text and post.author == "Stocks Under Rocks" and post.source == "rss"


def test_stocktwits_maps_messages_and_keeps_author_sentiment():
    payload = {"messages": [
        {"id": 666937081, "body": "$NVDA thks for the news ajax!!!", "created_at": "2026-10-20T16:52:53Z",
         "user": {"username": "Crakerjac"}, "entities": {"sentiment": {"basic": "Bullish"}}},
        {"id": 2, "body": "$NVDA meh", "created_at": "2026-10-20T16:50:00Z",
         "user": {"username": "quiet"}, "entities": {"sentiment": None}}]}
    src = mock(StockTwitsAPISource("$nvda"), json=payload)
    assert src.url.endswith("/streams/symbol/NVDA.json")
    a, b = list(src.fetch())
    assert (a.source, a.author, a.external_id) == ("stocktwits", "Crakerjac", "666937081")
    assert a.meta == {"author_sentiment": "Bullish"} and b.meta is None
    assert a.posted_at.isoformat() == "2026-10-20T16:52:53+00:00"
    assert StockTwitsAPISource("trending").url.endswith("/streams/trending.json")


@pytest.mark.parametrize("kind,target", [("reddit", "no spaces allowed"), ("reddit", "x"),
                                         ("stocktwits", "NOT A SYMBOL!"), ("rss", "http://insecure.example/feed"),
                                         ("substack", "bad name!"),
                                         ("myspace", "x")])
def test_invalid_sources_are_rejected(kind, target):
    with pytest.raises(SourceError):
        build_source(kind, target)


class FakeSource(Source):
    kind, target = "fake", "demo"

    def fetch(self):
        yield PostIn(text="Long $NVDA into earnings", author="alice", source="fake", external_id="1")
        yield PostIn(text="Nice weather today", author="bob", source="fake", external_id="2")


@pytest.fixture
def client(sf):
    from tally.api import routes
    from tally.api.main import create_app
    routes._submit_limit = None
    with TestClient(create_app()) as c:
        from tally.api.deps import get_runtime
        get_runtime().build_source = lambda kind, target, limit: FakeSource()   # dependency injection
        yield c


ADMIN = {"X-Admin-Token": "test-token"}


def test_preview_saves_nothing_and_ingest_saves_only_kept(client):
    body = {"kind": "reddit", "target": "stocks", "limit": 5}
    assert client.post("/api/sources/preview", json=body).status_code == 401
    prev = client.post("/api/sources/preview", json=body, headers=ADMIN).json()
    assert prev["fetched"] == 2 and prev["kept"] == 1
    assert client.get("/api/posts").json() == []                     # dry run wrote nothing
    res = client.post("/api/sources/ingest", json=body, headers=ADMIN).json()
    assert (res["new"], res["filtered"]) == (1, 1)
    again = client.post("/api/sources/ingest", json=body, headers=ADMIN).json()
    assert again["duplicate"] == 1 and again["new"] == 0             # re-polling is idempotent
    assert client.get("/api/sources", headers=ADMIN).json()["suggested"]["reddit"][0] == "stocks"
