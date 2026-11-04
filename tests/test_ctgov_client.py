"""Client tests with a fake HTTP server (httpx.MockTransport). No internet needed."""
import httpx
import pytest

from app.ctgov.client import CTGovClient, CTGovError, RateLimiter
from app.ctgov.query_builder import ApiQuery
from app.schemas.plan import Filters

QUERY = ApiQuery(label=None, filters=Filters(drug="x"), params={"query.intr": "x"})


def make(handler, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CTGovClient(http=http, rate_limiter=RateLimiter(1000), backoff_base=0, **kw)


def page(ids, total, token=None):
    body = {"totalCount": total, "studies": [{"id": i} for i in ids]}
    if token:
        body["nextPageToken"] = token
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_follows_pages_until_done():
    def handler(req):
        return page([3], 3) if req.url.params.get("pageToken") == "p2" else page([1, 2], 3, "p2")

    async with make(handler) as client:
        r = await client.fetch(QUERY, max_trials=100)
    assert [s["id"] for s in r.studies] == [1, 2, 3]
    assert r.total_count == 3 and not r.truncated and r.pages_fetched == 2


@pytest.mark.asyncio
async def test_stops_at_max_trials_and_marks_truncated():
    async with make(lambda req: page([1, 2], 10, "more")) as client:
        r = await client.fetch(QUERY, max_trials=2)
    assert len(r.studies) == 2 and r.truncated and r.warnings


@pytest.mark.asyncio
async def test_retries_on_503_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else page([1], 1)

    async with make(handler) as client:
        r = await client.fetch(QUERY, max_trials=10)
    assert len(calls) == 3 and len(r.studies) == 1


@pytest.mark.asyncio
async def test_400_fails_fast_without_retry():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, text="bad filter")

    async with make(handler) as client:
        with pytest.raises(CTGovError):
            await client.fetch(QUERY, max_trials=10)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_gives_up_after_max_retries():
    async with make(lambda req: httpx.Response(500), max_retries=2) as client:
        with pytest.raises(CTGovError):
            await client.fetch(QUERY, max_trials=10)


@pytest.mark.asyncio
async def test_broad_count_is_reported():
    query = ApiQuery(label=None, filters=Filters(drug="x"),
                     params={"filter.advanced": "strict"}, broad_params={"query.intr": "x"})

    def handler(req):
        if "query.intr" in req.url.params:
            assert req.url.params["pageSize"] == "1"
            return page([], 30)
        return page([1, 2], 2)

    async with make(handler) as client:
        r = await client.fetch(query, max_trials=10)
    assert r.total_count == 2 and r.broad_total_count == 30


@pytest.mark.asyncio
async def test_broad_count_failure_is_only_a_warning():
    query = ApiQuery(label=None, filters=Filters(drug="x"),
                     params={"filter.advanced": "strict"}, broad_params={"query.intr": "x"})

    def handler(req):
        return httpx.Response(400) if "query.intr" in req.url.params else page([1], 1)

    async with make(handler) as client:
        r = await client.fetch(query, max_trials=10)
    assert len(r.studies) == 1 and r.broad_total_count is None and r.warnings