"""Exact bucket counts from the API, for when the download was truncated.

A broad question ("cancer trials per year") can match far more trials than we
download, and counting the downloaded records then undercounts every bucket.
For a single-valued dimension whose DimensionSpec has an `api_condition`, the API
can count each value directly: one tiny request (pageSize=1, countTotal=true) per value.

Which values to count is decided by the caller from the data (values seen in the
downloaded sample, or the years of a time series), not from fixed lists here.
"""

import asyncio

from app.aggregation.dimensions import DIMENSIONS
from app.aggregation.models import ExactCounts
from app.schemas.common import Dimension

from .client import CTGovClient
from .query_builder import ApiQuery


def can_count(dim: Dimension | None) -> bool:
    spec = DIMENSIONS.get(dim)
    return bool(spec and spec.api_condition and not spec.multi_valued)


def _base_params(query: ApiQuery) -> dict[str, str]:
    return {k: v for k, v in query.params.items() if k not in ("fields", "sort")}


def count_params(query: ApiQuery, dim: Dimension, key: str) -> dict[str, str]:
    """The query's own filters AND the dimension's condition for this value."""
    params = _base_params(query)
    condition = DIMENSIONS[dim].api_condition(key)
    params["filter.advanced"] = " AND ".join(c for c in (params.get("filter.advanced"), condition) if c)
    return params


async def first_start_year(client: CTGovClient, query: ApiQuery) -> int | None:
    """Earliest start year among all matching trials (one tiny request)."""
    study = await client.first({**_base_params(query), "sort": "StartDate:asc"}, fields="NCTId,StartDate")
    raw = (((study or {}).get("protocolSection") or {}).get("statusModule") or {}) \
        .get("startDateStruct", {}).get("date") or ""
    return int(raw[:4]) if raw[:4].isdigit() else None


async def exact_counts(client: CTGovClient, query: ApiQuery, dim: Dimension, keys: list[str],
                       total: int) -> ExactCounts:
    results = await asyncio.gather(*(client.count(count_params(query, dim, k)) for k in keys))
    return ExactCounts(counts=dict(zip(keys, results)), total=total)
