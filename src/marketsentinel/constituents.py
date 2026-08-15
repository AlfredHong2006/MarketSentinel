"""Current S&P 500 and FTSE 100 constituent discovery with a transparent fallback."""

import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from marketsentinel.domain import Constituent, UniverseResult
from marketsentinel.errors import ConstituentNotFoundError, ProviderError
from marketsentinel.timeutils import utc_now

LOGGER = logging.getLogger(__name__)
_FOOTNOTE = re.compile(r"\[[^]]+]$")

_SOURCES = {
    "S&P 500": (
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "Symbol",
        "Security",
        400,
    ),
    "FTSE 100": (
        "https://en.wikipedia.org/wiki/FTSE_100_Index",
        "Ticker",
        "Company",
        80,
    ),
}

# Limited, reviewed names used only to improve search recall for well-known renamed entities.
_CONTROLLED_ALIASES: dict[str, tuple[str, ...]] = {
    "GOOGL": ("Google",),
    "META": ("Facebook",),
    "NVDA": ("Nvidia",),
    "BRK.B": ("Berkshire Hathaway",),
    "AZN": ("AstraZeneca PLC",),
    "HSBA": ("HSBC",),
    "SHEL": ("Shell plc",),
}

# This is deliberately only an offline demo subset, never presented as the full current universe.
_FALLBACK = (
    Constituent(symbol="AAPL", yahoo_symbol="AAPL", name="Apple Inc.", market="S&P 500"),
    Constituent(symbol="MSFT", yahoo_symbol="MSFT", name="Microsoft", market="S&P 500"),
    Constituent(symbol="NVDA", yahoo_symbol="NVDA", name="NVIDIA", market="S&P 500"),
    Constituent(symbol="AMZN", yahoo_symbol="AMZN", name="Amazon", market="S&P 500"),
    Constituent(symbol="GOOGL", yahoo_symbol="GOOGL", name="Alphabet", market="S&P 500"),
    Constituent(symbol="META", yahoo_symbol="META", name="Meta Platforms", market="S&P 500"),
    Constituent(symbol="TSLA", yahoo_symbol="TSLA", name="Tesla", market="S&P 500"),
    Constituent(symbol="AZN", yahoo_symbol="AZN.L", name="AstraZeneca", market="FTSE 100"),
    Constituent(symbol="SHEL", yahoo_symbol="SHEL.L", name="Shell", market="FTSE 100"),
    Constituent(symbol="HSBA", yahoo_symbol="HSBA.L", name="HSBC Holdings", market="FTSE 100"),
    Constituent(symbol="ULVR", yahoo_symbol="ULVR.L", name="Unilever", market="FTSE 100"),
    Constituent(symbol="BP", yahoo_symbol="BP.L", name="BP", market="FTSE 100"),
    Constituent(symbol="GSK", yahoo_symbol="GSK.L", name="GSK", market="FTSE 100"),
)


class WikipediaConstituentService:
    """Fetch, validate, cache, search, and resolve supported index constituents."""

    def __init__(
        self,
        cache_path: Path,
        timeout_seconds: float = 10.0,
        user_agent: str = "MarketSentinel/0.1",
        cache_ttl: timedelta = timedelta(hours=24),
    ) -> None:
        self.cache_path = cache_path
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.cache_ttl = cache_ttl
        self._memory_cache: UniverseResult | None = None

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=4),
        reraise=True,
    )
    def _fetch_market(self, market: str) -> list[Constituent]:
        url, symbol_column, name_column, expected_minimum = _SOURCES[market]
        response = httpx.get(
            url,
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table", id="constituents")
        if table is None:
            raise ProviderError(f"{market} page did not contain the expected constituents table")

        rows = table.find_all("tr")
        if not rows:
            raise ProviderError(f"{market} constituents table did not contain any rows")
        headers = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])]
        try:
            symbol_index = headers.index(symbol_column)
            name_index = headers.index(name_column)
        except ValueError as exc:
            raise ProviderError(f"{market} constituent columns changed: {headers}") from exc

        constituents: list[Constituent] = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if max(symbol_index, name_index) >= len(cells):
                continue
            symbol = _FOOTNOTE.sub("", cells[symbol_index].get_text(" ", strip=True)).strip()
            name = _FOOTNOTE.sub("", cells[name_index].get_text(" ", strip=True)).strip()
            if not symbol or not name:
                continue
            yahoo_symbol = symbol.replace(".", "-") if market == "S&P 500" else f"{symbol}.L"
            constituents.append(
                Constituent(
                    symbol=symbol,
                    yahoo_symbol=yahoo_symbol,
                    name=name,
                    market=market,
                    aliases=_CONTROLLED_ALIASES.get(symbol, ()),
                )
            )

        if len(constituents) < expected_minimum:
            raise ProviderError(
                f"{market} returned only {len(constituents)} valid constituents; "
                f"expected at least {expected_minimum}"
            )
        return constituents

    def _read_cache(self, allow_stale: bool = False) -> UniverseResult | None:
        if not self.cache_path.exists():
            return None
        age = utc_now() - datetime.fromtimestamp(self.cache_path.stat().st_mtime, tz=UTC)
        if not allow_stale and age > self.cache_ttl:
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return UniverseResult.model_validate(payload)
        except (OSError, ValueError):
            LOGGER.exception("Unable to read constituent cache at %s", self.cache_path)
            return None

    def _write_cache(self, result: UniverseResult) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix="constituents-",
            suffix=".tmp",
            dir=self.cache_path.parent,
            text=True,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as temporary_file:
                json.dump(result.model_dump(mode="json"), temporary_file, indent=2)
            os.replace(temporary_name, self.cache_path)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def load(self, force_refresh: bool = False) -> UniverseResult:
        if self._memory_cache is not None and not force_refresh:
            return self._memory_cache
        if not force_refresh and (cached := self._read_cache()) is not None:
            self._memory_cache = cached
            return cached

        try:
            constituents: list[Constituent] = []
            for market in _SOURCES:
                constituents.extend(self._fetch_market(market))
            result = UniverseResult(
                constituents=sorted(constituents, key=lambda item: (item.market, item.name)),
                source="Wikipedia constituent tables",
                is_fallback=False,
                fetched_at=utc_now(),
            )
            self._write_cache(result)
        except (httpx.HTTPError, ProviderError):
            LOGGER.exception("Live constituent refresh failed")
            stale = self._read_cache(allow_stale=True)
            if stale is not None:
                result = stale.model_copy(
                    update={
                        "is_fallback": True,
                        "message": "Live refresh failed; using the last cached universe.",
                    }
                )
            else:
                result = UniverseResult(
                    constituents=list(_FALLBACK),
                    source="built-in offline demo subset",
                    is_fallback=True,
                    fetched_at=utc_now(),
                    message="Live constituent data is unavailable; this is not the full index.",
                )
        self._memory_cache = result
        return result

    def search(self, query: str = "", market: str | None = None, limit: int = 20) -> UniverseResult:
        universe = self.load()
        normalized_query = query.casefold().strip()
        candidates = [
            item
            for item in universe.constituents
            if market in (None, "All", item.market)
            and (
                not normalized_query
                or normalized_query in item.symbol.casefold()
                or normalized_query in item.name.casefold()
            )
        ]

        def rank(item: Constituent) -> tuple[int, str]:
            symbol = item.symbol.casefold()
            name = item.name.casefold()
            if normalized_query and symbol == normalized_query:
                return (0, name)
            if normalized_query and (
                symbol.startswith(normalized_query) or name.startswith(normalized_query)
            ):
                return (1, name)
            return (2, name)

        return universe.model_copy(update={"constituents": sorted(candidates, key=rank)[:limit]})

    def resolve(self, symbol: str) -> Constituent:
        normalized = symbol.casefold().strip()
        universe = self.load()
        for item in universe.constituents:
            if normalized in {item.symbol.casefold(), item.yahoo_symbol.casefold()}:
                return item
        raise ConstituentNotFoundError(f"{symbol!r} is not in the available constituent universe")
