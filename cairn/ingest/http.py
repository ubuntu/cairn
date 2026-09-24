"""HTTP with explicit timeouts, bounded retries and conditional GET."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "cairn (+https://github.com/cairn)"


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Read timeout is deliberately short: Launchpad has been observed
    stalling for ~270 s, and a retry almost always answers immediately."""

    connect: float = 10.0
    read: float = 25.0

    def as_tuple(self) -> tuple[float, float]:
        return (self.connect, self.read)


@dataclass(frozen=True, slots=True)
class Retries:
    attempts: int = 4
    backoff_cap: float = 4.0
    jitter: float = 0.4
    max_retry_after: float = 30.0


@dataclass(frozen=True, slots=True)
class CacheEntry:
    body: bytes
    etag: str | None = None
    last_modified: str | None = None


class Cache(Protocol):
    def load(self, url: str) -> CacheEntry | None: ...
    def store(self, url: str, entry: CacheEntry) -> None: ...


class FileCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self.directory / f"{key}.body", self.directory / f"{key}.json"

    def load(self, url: str) -> CacheEntry | None:
        body_path, meta_path = self._paths(url)
        if not (body_path.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text())
            return CacheEntry(
                body=body_path.read_bytes(),
                etag=meta.get("etag"),
                last_modified=meta.get("last_modified"),
            )
        except (OSError, ValueError) as exc:
            log.warning("discarding unreadable cache entry for %s: %s", url, exc)
            return None

    def store(self, url: str, entry: CacheEntry) -> None:
        body_path, meta_path = self._paths(url)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(entry.body)
            meta_path.write_text(
                json.dumps({"etag": entry.etag, "last_modified": entry.last_modified})
            )
        except OSError as exc:
            log.warning("could not cache %s: %s", url, exc)


class HttpFetcher:
    """Implements the Fetcher protocol in ingest.base.

    urllib3's own retrying is disabled so that stalls and retryable statuses
    go through a single visible code path.
    """

    def __init__(
        self,
        *,
        timeouts: Timeouts | None = None,
        retries: Retries | None = None,
        cache: Cache | None = None,
        session: requests.Session | None = None,
        user_agent: str = USER_AGENT,
        sleep=time.sleep,
    ) -> None:
        self.timeouts = timeouts or Timeouts()
        self.retries = retries or Retries()
        self.cache = cache
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", user_agent)
        for scheme in ("http://", "https://"):
            self.session.mount(scheme, HTTPAdapter(max_retries=0))

    def get(self, url: str) -> bytes:
        cached = self.cache.load(url) if self.cache else None
        response = self._request(url, self._conditional_headers(cached))

        if response.status_code == 304:
            if cached is None:
                raise FetchError(f"304 without a cached body: {url}")
            return cached.body

        body = response.content
        if self.cache is not None:
            self.cache.store(
                url,
                CacheEntry(
                    body=body,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                ),
            )
        return body

    @staticmethod
    def _conditional_headers(cached: CacheEntry | None) -> dict[str, str]:
        if cached is None:
            return {}
        headers = {}
        if cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        return headers

    def _request(self, url: str, headers: dict[str, str]) -> requests.Response:
        last: Exception | None = None

        for attempt in range(1, self.retries.attempts + 1):
            retryable: requests.Response | None = None
            try:
                response = self.session.get(
                    url, headers=headers, timeout=self.timeouts.as_tuple()
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    if response.status_code >= 400:
                        raise FetchError(f"HTTP {response.status_code}: {url}")
                    return response
                retryable = response
                last = FetchError(f"HTTP {response.status_code}: {url}")

            if attempt < self.retries.attempts:
                delay = self._delay(attempt, retryable)
                log.debug(
                    "retry %d/%d in %.1fs: %s",
                    attempt,
                    self.retries.attempts,
                    delay,
                    url,
                )
                self._sleep(delay)

        raise FetchError(
            f"giving up after {self.retries.attempts} attempts: {url}"
        ) from last

    def _delay(self, attempt: int, response: requests.Response | None) -> float:
        retry_after = self._retry_after(response)
        if retry_after is not None:
            return min(retry_after, self.retries.max_retry_after)
        backoff = min(2 ** (attempt - 1), self.retries.backoff_cap)
        return backoff + random.uniform(0, self.retries.jitter)

    @staticmethod
    def _retry_after(response: requests.Response | None) -> float | None:
        if response is None or response.status_code != 429:
            return None
        try:
            return float(response.headers.get("Retry-After", ""))
        except ValueError:
            return None
