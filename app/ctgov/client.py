"""ClinicalTrials.gov API v2 client.

- Pagination: follows nextPageToken until all matches (or max_trials) are fetched.
- Retry: 429 / 5xx / network errors are retried with exponential backoff.
  Other 4xx (bad parameters) fail immediately: retrying won't fix them.
- Rate limit: at most N requests per minute, shared across concurrent queries.
- Cache: optional file cache keyed by the exact request parameters.
"""

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from .cache import FileCache
from .query_builder import ApiQuery

log = logging.getLogger(__name__)

BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
MAX_PAGE_SIZE = 1000
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class CTGovError(Exception):
    """The API could not be reached or rejected the request."""


@dataclass
class FetchResult:
    query: ApiQuery
    studies: list[dict]
    total_count: int          # how many trials the API says match
    truncated: bool           # True if we stopped at max_trials
    request_url: str          # first-page URL, for traceability in the response
    broad_total_count: int | None = None  # broad search total (incl. text-only mentions)
    pages_fetched: int = 0
    cache_hits: int = 0
    warnings: list[str] = field(default_factory=list)


class RateLimiter:
    """Sliding window: at most `max_calls` in any `period` seconds."""

    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                await asyncio.sleep(self.period - (now - self._calls[0]))


class CTGovClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        cache: FileCache | None = None,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        timeout: float = 30.0,
    ):
        self.http = http or httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": "ctviz/0.1 (take-home project)"}
        )
        self.cache = cache
        self.limiter = rate_limiter or RateLimiter(max_calls=50)
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()

    # ------------------------------------------------------------ public

    async def fetch_all(self, queries: list[ApiQuery], max_trials: int) -> list[FetchResult]:
        """Run several queries concurrently (e.g. one per compare group)."""
        return list(await asyncio.gather(*(self.fetch(q, max_trials) for q in queries)))

    async def fetch(self, query: ApiQuery, max_trials: int) -> FetchResult:
        params = {
            **query.params,
            "pageSize": str(min(MAX_PAGE_SIZE, max_trials)),
            "countTotal": "true",
        }
        result = FetchResult(
            query=query, studies=[], total_count=0, truncated=False,
            request_url=str(httpx.URL(BASE_URL, params=query.params)),
        )

        page_token = None
        while True:
            page_params = {**params, "pageToken": page_token} if page_token else params
            data, from_cache = await self._get_page(page_params)
            result.pages_fetched += 1
            result.cache_hits += from_cache
            if page_token is None:
                result.total_count = data.get("totalCount", 0)
            result.studies.extend(data.get("studies", []))

            page_token = data.get("nextPageToken")
            if not page_token or len(result.studies) >= max_trials:
                break

        result.studies = result.studies[:max_trials]
        result.truncated = result.total_count > len(result.studies)
        if result.truncated:
            result.warnings.append(
                f"Only the first {len(result.studies)} of {result.total_count} matching trials were used."
            )
        if query.broad_params is not None:
            try:
                result.broad_total_count = await self.count(query.broad_params)
            except CTGovError as exc:  # metadata only: never fail the request over it
                result.warnings.append(f"Could not get broad-search count: {exc}")

        log.info("fetched %d/%d trials in %d pages (%d cached) for %s",
                 len(result.studies), result.total_count, result.pages_fetched,
                 result.cache_hits, query.label or "query")
        return result

    async def count(self, params: dict[str, str]) -> int:
        """Total matches for a search, without downloading the trials (one tiny request)."""
        data, _ = await self._get_page({**params, "pageSize": "1", "countTotal": "true", "fields": "NCTId"})
        return data.get("totalCount", 0)

    async def first(self, params: dict[str, str], fields: str) -> dict | None:
        """The first matching study for these params (use a `sort` param to choose which)."""
        data, _ = await self._get_page({**params, "pageSize": "1", "fields": fields})
        studies = data.get("studies") or []
        return studies[0] if studies else None

    # ------------------------------------------------------------ internals

    async def _get_page(self, params: dict[str, str]) -> tuple[dict, bool]:
        if self.cache and (cached := self.cache.get(params)) is not None:
            return cached, True

        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            retry_after = None
            await self.limiter.acquire()
            try:
                resp = await self.http.get(BASE_URL, params=params)
            except httpx.TransportError as exc:  # timeout, connection reset, DNS ...
                last_error = f"network error: {exc!r}"
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        last_error = "response was not valid JSON"
                    else:
                        if self.cache:
                            self.cache.set(params, data)
                        return data, False
                elif resp.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {resp.status_code}"
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                else:
                    # e.g. 400 for an invalid filter: retrying won't help.
                    raise CTGovError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            if attempt < self.max_retries:
                delay = retry_after or self.backoff_base * (2 ** attempt + random.uniform(0, 0.5))
                log.warning("ctgov %s, retry %d/%d in %.1fs",
                            last_error, attempt + 1, self.max_retries, delay)
                await asyncio.sleep(delay)

        raise CTGovError(f"giving up after {self.max_retries + 1} attempts: {last_error}")


def _parse_retry_after(value: str | None) -> float | None:
    try:
        return min(float(value), 60.0) if value else None
    except ValueError:
        return None  # HTTP-date format; fall back to our own backoff


def make_client() -> CTGovClient:
    """Client configured from settings (used by the app and scripts)."""
    from app.config import settings
    cache = FileCache(settings.ctgov_cache_dir, settings.ctgov_cache_ttl_seconds) \
        if settings.ctgov_cache_enabled else None
    return CTGovClient(
        cache=cache,
        rate_limiter=RateLimiter(max_calls=settings.ctgov_requests_per_minute),
        max_retries=settings.ctgov_max_retries,
        timeout=settings.ctgov_timeout_seconds,
    )