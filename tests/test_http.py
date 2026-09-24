from __future__ import annotations

import pytest
import requests

from cairn.ingest.base import Fetcher
from cairn.ingest.http import (
    CacheEntry,
    FetchError,
    FileCache,
    HttpFetcher,
    Retries,
    Timeouts,
)


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeSession:
    """Replays a scripted sequence and records what was sent."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.mounted: list[str] = []

    def mount(self, prefix, adapter):
        self.mounted.append(prefix)
        self.adapter = adapter

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fetcher(*outcomes, **kw):
    session = FakeSession(*outcomes)
    kw.setdefault("sleep", lambda _: None)
    kw.setdefault("retries", Retries(attempts=3, jitter=0.0))
    return HttpFetcher(session=session, **kw), session


class TestProtocol:
    def test_satisfies_fetcher_protocol(self):
        f, _ = fetcher(FakeResponse())
        assert isinstance(f, Fetcher)

    def test_disables_urllib3_retries(self):
        _, session = fetcher(FakeResponse())
        assert session.adapter.max_retries.total == 0
        assert session.mounted == ["http://", "https://"]


class TestTimeouts:
    def test_every_request_has_explicit_timeouts(self):
        f, session = fetcher(FakeResponse(content=b"x"))
        f.get("http://example/a")
        assert session.calls[0]["timeout"] == (10.0, 25.0)

    def test_timeouts_are_configurable(self):
        f, session = fetcher(FakeResponse(), timeouts=Timeouts(connect=1, read=2))
        f.get("http://example/a")
        assert session.calls[0]["timeout"] == (1, 2)


class TestRetries:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retries_retryable_statuses(self, status):
        f, session = fetcher(FakeResponse(status), FakeResponse(200, b"ok"))
        assert f.get("http://example/a") == b"ok"
        assert len(session.calls) == 2

    @pytest.mark.parametrize("exc", [requests.Timeout(), requests.ConnectionError()])
    def test_retries_stalls(self, exc):
        f, session = fetcher(exc, FakeResponse(200, b"ok"))
        assert f.get("http://example/a") == b"ok"
        assert len(session.calls) == 2

    def test_does_not_retry_client_errors(self):
        f, session = fetcher(FakeResponse(404))
        with pytest.raises(FetchError, match="404"):
            f.get("http://example/a")
        assert len(session.calls) == 1

    def test_gives_up_after_bounded_attempts(self):
        f, session = fetcher(*[FakeResponse(503)] * 3)
        with pytest.raises(FetchError, match="giving up"):
            f.get("http://example/a")
        assert len(session.calls) == 3

    def test_backoff_is_bounded_and_grows(self):
        delays = []
        f, _ = fetcher(
            *[FakeResponse(503)] * 3,
            sleep=delays.append,
            retries=Retries(attempts=3, backoff_cap=4.0, jitter=0.0),
        )
        with pytest.raises(FetchError):
            f.get("http://example/a")
        assert delays == [1.0, 2.0]

    def test_honours_retry_after_on_429(self):
        delays = []
        f, _ = fetcher(
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(200, b"ok"),
            sleep=delays.append,
        )
        assert f.get("http://example/a") == b"ok"
        assert delays == [7.0]

    def test_retry_after_is_capped(self):
        delays = []
        f, _ = fetcher(
            FakeResponse(429, headers={"Retry-After": "9999"}),
            FakeResponse(200, b"ok"),
            sleep=delays.append,
            retries=Retries(attempts=3, jitter=0.0, max_retry_after=30.0),
        )
        f.get("http://example/a")
        assert delays == [30.0]

    def test_garbage_retry_after_falls_back_to_backoff(self):
        delays = []
        f, _ = fetcher(
            FakeResponse(429, headers={"Retry-After": "soon"}),
            FakeResponse(200, b"ok"),
            sleep=delays.append,
        )
        f.get("http://example/a")
        assert delays == [1.0]


class TestConditionalGet:
    def test_no_conditional_headers_without_cache(self):
        f, session = fetcher(FakeResponse(200, b"x"))
        f.get("http://example/a")
        assert session.calls[0]["headers"] == {}

    def test_sends_validators_from_cache(self, tmp_path):
        cache = FileCache(tmp_path)
        cache.store(
            "http://example/a", CacheEntry(b"old", etag='"e1"', last_modified="Mon")
        )
        f, session = fetcher(FakeResponse(304), cache=cache)
        assert f.get("http://example/a") == b"old"
        assert session.calls[0]["headers"] == {
            "If-None-Match": '"e1"',
            "If-Modified-Since": "Mon",
        }

    def test_200_replaces_cached_body(self, tmp_path):
        cache = FileCache(tmp_path)
        cache.store("http://example/a", CacheEntry(b"old", etag='"e1"'))
        f, _ = fetcher(FakeResponse(200, b"new", {"ETag": '"e2"'}), cache=cache)
        assert f.get("http://example/a") == b"new"
        assert cache.load("http://example/a").etag == '"e2"'

    def test_304_without_cached_body_is_an_error(self):
        f, _ = fetcher(FakeResponse(304))
        with pytest.raises(FetchError, match="304"):
            f.get("http://example/a")


class TestFileCache:
    def test_round_trip(self, tmp_path):
        cache = FileCache(tmp_path)
        cache.store("http://example/a", CacheEntry(b"body", etag='"e"'))
        entry = cache.load("http://example/a")
        assert entry.body == b"body"
        assert entry.etag == '"e"'

    def test_miss_returns_none(self, tmp_path):
        assert FileCache(tmp_path).load("http://example/nope") is None

    def test_corrupt_metadata_is_discarded_not_raised(self, tmp_path):
        cache = FileCache(tmp_path)
        cache.store("http://example/a", CacheEntry(b"body"))
        next(tmp_path.glob("*.json")).write_text("{not json")
        assert cache.load("http://example/a") is None

    def test_urls_do_not_collide(self, tmp_path):
        cache = FileCache(tmp_path)
        cache.store("http://example/a", CacheEntry(b"a"))
        cache.store("http://example/b", CacheEntry(b"b"))
        assert cache.load("http://example/a").body == b"a"
        assert cache.load("http://example/b").body == b"b"
