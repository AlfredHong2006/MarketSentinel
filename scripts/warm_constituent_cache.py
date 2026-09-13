"""Ensure the constituent cache exists before an offline step needs it.

``run_coverage_cycle.py --mode activate`` resolves companies from the local constituent cache
only -- deliberately, it never fetches live (see coverage_cycle.py / build_coverage_service). On a
brand-new deployment, or if the scheduled workflow's private R2 state has never been seeded, that
cache file does not exist yet and activation would fail with nothing to fall back to.

This script performs the one live Wikipedia fetch such a cold start needs, exactly once, so the
activate step that follows it can stay offline as designed. A no-op when the cache file is already
present (the normal case: the workflow downloads it from R2 before this runs).

Usage:
    uv run python scripts/warm_constituent_cache.py NVDA PFE
"""

import sys

from marketsentinel.config import get_settings
from marketsentinel.constituents import WikipediaConstituentService


def main(argv: list[str] | None = None) -> int:
    tickers = sys.argv[1:] if argv is None else argv
    if not tickers:
        print("usage: warm_constituent_cache.py TICKER [TICKER ...]", file=sys.stderr)
        return 2

    settings = get_settings()
    if settings.constituent_cache_path.is_file():
        print(f"constituent cache present at {settings.constituent_cache_path}")
        return 0

    service = WikipediaConstituentService(
        cache_path=settings.constituent_cache_path,
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
    )
    for ticker in tickers:
        constituent = service.resolve(ticker.strip().upper())
        print(f"resolved {constituent.symbol}: {constituent.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
