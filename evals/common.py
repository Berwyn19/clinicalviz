"""Small helpers shared by the eval scripts."""

import json
import math
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a proportion k/n (Wilson score interval)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def pct(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f}% ({k}/{n}, 95% CI {100 * lo:.0f}-{100 * hi:.0f}%)"


def norm_text(value) -> str | None:
    """Case/punctuation-insensitive form of a text filter value."""
    if value is None:
        return None
    value = str(value).replace("’", "'").lower()
    return " ".join(re.sub(r"[^\w\s']", " ", value).split()) or None


def save(name: str, data) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    return path
