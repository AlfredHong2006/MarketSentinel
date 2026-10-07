"""MR-007 read-only probe: how deep can GDELT DOC 2.0 and Google News RSS reach?

One script, one mode per question. Every network mode makes read-only GETs to exactly two
endpoints (GDELT DOC 2.0 and Google News RSS search), paces them, counts them, and stops at once on
a rate-limit / block / terms-of-use response. Nothing is written to any database; the only file
written is the optional ``--out`` JSON of probe rows. ``overlap`` opens the stored corpus with
SQLite URI ``mode=ro`` and makes no network call.

Modes
    gdelt-diag      Q1: reproduce and separate the GDELT failure (3 HTTP GETs + 3 socket checks).
    gdelt-windows   Q2: dated windows at ~3/6/12/24/36 months for NVDA, PFE, AZN (15 GETs).
    google-windows  Q3: dated 30-day windows at 13/18/24/36 months for NVDA, PFE (8 GETs).
    google-shorter  Q3: one 30-day window versus four 7-day windows at one age (5 GETs).
    overlap         Q2: compare saved GDELT rows with the stored corpus (read-only DB, 0 GETs).
    gkg-sample      Q2b: ONE GDELT GKG 2.x 15-minute file from data.gdeltproject.org (1 GET).
    summarize       Re-read an --out file offline (0 GETs).
"""

import argparse
import json
import socket
import ssl
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import feedparser
import httpx

from marketsentinel.domain import Constituent
from marketsentinel.normalization import (
    normalize_text,
    normalize_url,
    relevance_score,
)
from marketsentinel.sources.historical import (
    _GDELT_DOC_URL,
    _gdelt_query,
    _google_historical_query,
    _parse_gdelt_datetime,
)

NL, TAB = chr(10), chr(9)
GDELT_INTERVAL = 5.25  # the repo's configured request_interval_seconds
GOOGLE_INTERVAL = 5.25  # same pace, deliberately no faster
USER_AGENT = "MarketSentinel/0.1"
GDELT_HOST = "api.gdeltproject.org"
RELEVANCE_THRESHOLD = 0.5
PACIFIC = ZoneInfo("America/Los_Angeles")
DB_PATH = Path(r"C:\Dev\MarketSentinel\data\marketsentinel.db")

COMPANIES = {
    "NVDA": Constituent(symbol="NVDA", yahoo_symbol="NVDA", name="NVIDIA", market="S&P 500"),
    "PFE": Constituent(symbol="PFE", yahoo_symbol="PFE", name="Pfizer", market="S&P 500"),
    "AZN": Constituent(symbol="AZN", yahoo_symbol="AZN.L", name="AstraZeneca", market="FTSE 100"),
}
BLOCK_MARKERS = ("terms of use", "rate limit", "too many requests", "blocked", "captcha")


class Stop(Exception):
    """Raised on a rate-limit, block or terms-of-use response; the run ends immediately."""


class Client:
    """Counts every GET, paces per host, and refuses to continue after a block signal."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.requests = 0
        self._last = 0.0

    def get(self, url: str, params: dict, timeout: float, accept: str) -> httpx.Response:
        wait = self.interval - (time.monotonic() - self._last)
        if self._last and wait > 0:
            time.sleep(wait)
        self.requests += 1
        started = time.monotonic()
        try:
            response = httpx.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": accept},
                timeout=timeout,
                follow_redirects=True,
            )
        finally:
            self._last = time.monotonic()
        response.elapsed_s = round(time.monotonic() - started, 2)  # type: ignore[attr-defined]
        if response.status_code in (403, 429) or any(
            marker in response.text[:2000].lower() for marker in BLOCK_MARKERS
        ):
            raise Stop(f"HTTP {response.status_code}: {response.text[:200]!r}")
        return response


def window_end(months: int, now: datetime) -> datetime:
    return now - timedelta(days=30 * months)


# --------------------------------------------------------------------------------------- Q1
def gdelt_diag(client: Client, now: datetime) -> dict:
    result: dict = {"checks": {}}
    try:
        infos = socket.getaddrinfo(GDELT_HOST, 443, type=socket.SOCK_STREAM)
        result["checks"]["dns"] = {"ok": True, "addresses": sorted({i[4][0] for i in infos})}
    except OSError as exc:
        result["checks"]["dns"] = {"ok": False, "error": repr(exc)}
        return result
    started = time.monotonic()
    try:
        with socket.create_connection((GDELT_HOST, 443), timeout=30) as raw:
            tcp_s = round(time.monotonic() - started, 2)
            result["checks"]["tcp"] = {"ok": True, "seconds": tcp_s}
            t1 = time.monotonic()
            with ssl.create_default_context().wrap_socket(raw, server_hostname=GDELT_HOST) as tls:
                result["checks"]["tls"] = {
                    "ok": True,
                    "seconds": round(time.monotonic() - t1, 2),
                    "version": tls.version(),
                }
    except OSError as exc:
        key = "tls" if "tcp" in result["checks"] else "tcp"
        result["checks"][key] = {
            "ok": False,
            "error": repr(exc),
            "seconds": round(time.monotonic() - started, 2),
        }

    print(json.dumps({"connection_checks": result["checks"]}), flush=True)
    nvda = COMPANIES["NVDA"]
    start, end = now - timedelta(days=5), now
    fmt = "%Y%m%d%H%M%S"
    exact = {
        "query": _gdelt_query(nvda),
        "mode": "artlist",
        "format": "json",
        "maxrecords": 250,
        "sort": "datedesc",
        "startdatetime": start.strftime(fmt),
        "enddatetime": end.strftime(fmt),
    }
    minimal = {"query": "nvidia", "mode": "artlist", "format": "json", "maxrecords": 5}
    attempts = [
        ("exact provider request, 10s timeout", exact, 10.0),
        ("exact provider request, 30s timeout", exact, 30.0),
        ("minimal query 'nvidia' maxrecords=5, 30s timeout", minimal, 30.0),
    ]
    result["requests"] = []
    for label, params, timeout in attempts:
        entry: dict = {"label": label, "timeout": timeout, "query": params["query"]}
        try:
            response = client.get(_GDELT_DOC_URL, params, timeout, "application/json")
            entry.update(
                status=response.status_code,
                seconds=response.elapsed_s,  # type: ignore[attr-defined]
                content_type=response.headers.get("content-type"),
                bytes=len(response.content),
            )
            try:
                entry["articles"] = len(response.json().get("articles", []))
            except ValueError:
                entry["body_head"] = response.text[:200]
        except Stop:
            raise
        except httpx.HTTPError as exc:
            entry["error"] = type(exc).__name__
        result["requests"].append(entry)
        print(json.dumps(entry))
    return result


# --------------------------------------------------------------------------------------- Q2
def quality_row(article: dict, constituent: Constituent) -> dict:
    title = str(article.get("title", "")).strip()
    url = str(article.get("url", "")).strip()
    seen = _parse_gdelt_datetime(article.get("seendate"))
    return {
        "title": title,
        "url": url,
        "domain": str(article.get("domain") or urlsplit(url).netloc),
        "seendate": article.get("seendate"),
        "time_of_day": bool(seen and (seen.hour or seen.minute or seen.second)),
        "relevance": relevance_score(title, constituent),
        "language": article.get("language"),
    }


def gdelt_windows(client: Client, now: datetime, tickers: list[str], ages: list[int]) -> dict:
    out: dict = {"source": "gdelt", "windows": []}
    for symbol in tickers:
        constituent = COMPANIES[symbol]
        for age in ages:
            end = window_end(age, now)
            start = end - timedelta(days=5)
            params = {
                "query": _gdelt_query(constituent),
                "mode": "artlist",
                "format": "json",
                "maxrecords": 250,
                "sort": "datedesc",
                "startdatetime": start.strftime("%Y%m%d%H%M%S"),
                "enddatetime": end.strftime("%Y%m%d%H%M%S"),
            }
            entry = {
                "ticker": symbol,
                "age_months": age,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            try:
                response = client.get(_GDELT_DOC_URL, params, 30.0, "application/json")
                entry["status"] = response.status_code
                entry["rows"] = [
                    quality_row(a, constituent) for a in response.json().get("articles", [])
                ]
            except Stop:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                entry["error"] = type(exc).__name__
            out["windows"].append(entry)
            print(
                f"{symbol} {age}m: {entry.get('status')} rows={len(entry.get('rows', []))}"
                f" {entry.get('error', '')}"
            )
    return out


# --------------------------------------------------------------------------------------- Q3
def google_row(entry, constituent: Constituent) -> dict:
    parsed = getattr(entry, "published_parsed", None)
    published = datetime(*parsed[:6], tzinfo=UTC) if parsed else None
    title = str(entry.get("title", "")).strip()
    source = str(entry.get("source", {}).get("title", "Unknown source")).strip()
    if source != "Unknown source" and title.endswith(f" - {source}"):
        title = title[: -(len(source) + 3)].strip()
    pacific_midnight = bool(
        published and (lambda p: p.hour == p.minute == p.second == 0)(published.astimezone(PACIFIC))
    )
    return {
        "title": title,
        "url": str(entry.get("link", "")),
        "domain": source,
        "published": published.isoformat() if published else None,
        "pacific_midnight": pacific_midnight,
        "time_of_day": bool(published and not pacific_midnight),
        "relevance": relevance_score(title, constituent),
    }


def google_window(client: Client, constituent: Constituent, start: datetime, end: datetime) -> dict:
    query = _google_historical_query(constituent, start, end)
    entry: dict = {
        "ticker": constituent.symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "query": query,
    }
    try:
        response = client.get(
            "https://news.google.com/rss/search",
            {"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"},
            30.0,
            "application/rss+xml, application/xml",
        )
        entry["status"] = response.status_code
        entries = feedparser.parse(response.content).entries
        rows = [google_row(e, constituent) for e in entries]
        for row in rows:
            row["in_window"] = bool(
                row["published"] and start <= datetime.fromisoformat(row["published"]) <= end
            )
        entry["rows"] = rows
    except Stop:
        raise
    except httpx.HTTPError as exc:
        entry["error"] = type(exc).__name__
    return entry


def google_windows(client: Client, now: datetime, tickers: list[str], ages: list[int]) -> dict:
    out: dict = {"source": "google", "windows": []}
    for symbol in tickers:
        for age in ages:
            end = window_end(age, now)
            entry = google_window(client, COMPANIES[symbol], end - timedelta(days=30), end)
            entry["age_months"] = age
            out["windows"].append(entry)
            print(
                f"{symbol} {age}m: {entry.get('status')} entries={len(entry.get('rows', []))}"
                f" {entry.get('error', '')}"
            )
    return out


def google_shorter(client: Client, now: datetime, symbol: str, age: int) -> dict:
    out: dict = {"source": "google", "windows": []}
    constituent = COMPANIES[symbol]
    end = window_end(age, now)
    month_start = end - timedelta(days=30)
    whole = google_window(client, constituent, month_start, end)
    whole["age_months"], whole["shape"] = age, "30d"
    out["windows"].append(whole)
    for index in range(4):
        sub_start = month_start + timedelta(days=7 * index)
        sub_end = min(end, sub_start + timedelta(days=7))
        entry = google_window(client, constituent, sub_start, sub_end)
        entry["age_months"], entry["shape"] = age, f"7d#{index + 1}"
        out["windows"].append(entry)
    for entry in out["windows"]:
        print(f"{entry['shape']}: {entry.get('status')} entries={len(entry.get('rows', []))}")
    return out


# ------------------------------------------------------------------------------ summarising
def summarize(payload: dict) -> list[dict]:
    rows_out = []
    for window in payload["windows"]:
        rows = window.get("rows")
        if rows is None:
            rows_out.append({k: window.get(k) for k in ("ticker", "age_months", "error")})
            continue
        in_window = [r for r in rows if r.get("in_window", True)]
        relevant = [r for r in in_window if r["relevance"] >= RELEVANCE_THRESHOLD]
        valid_url = [r for r in rows if r["url"]]
        per_day: dict[str, set] = defaultdict(set)
        for r in relevant:
            stamp = r.get("seendate") or r.get("published") or ""
            day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if r.get("seendate") else stamp[:10]
            per_day[day].add(r["domain"])
        distinct_titles = {normalize_text(r["title"]) for r in relevant}
        rows_out.append(
            {
                "ticker": window["ticker"],
                "age_months": window["age_months"],
                "shape": window.get("shape"),
                "returned": len(rows),
                "in_window": len(in_window),
                "valid_url": len(valid_url),
                "relevant": len(relevant),
                "distinct_titles": len(distinct_titles),
                "with_time_of_day": sum(r["time_of_day"] for r in in_window),
                "pacific_midnight": sum(r.get("pacific_midnight", False) for r in in_window),
                "days_covered": len(per_day),
                "max_publishers_per_day": max((len(v) for v in per_day.values()), default=0),
                "days_with_3plus_publishers": sum(len(v) >= 3 for v in per_day.values()),
                "top_domains": Counter(r["domain"] for r in relevant).most_common(3),
            }
        )
    return rows_out


# ------------------------------------------------------------------------------------ overlap
def overlap(payload: dict) -> list[dict]:
    connection = __import__("sqlite3").connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    result = []
    try:
        for window in payload["windows"]:
            rows = [r for r in window.get("rows", []) if r["relevance"] >= RELEVANCE_THRESHOLD]
            start = datetime.fromisoformat(window["start"]).strftime("%Y-%m-%dT%H:%M:%S")
            end = datetime.fromisoformat(window["end"]).strftime("%Y-%m-%dT%H:%M:%S")
            stored = connection.execute(
                "SELECT normalized_title, normalized_url FROM articles "
                "WHERE ticker = ? AND published_at >= ? AND published_at <= ? AND is_demo = 0",
                (window["ticker"], start, end),
            ).fetchall()
            titles = {t for t, _ in stored}
            urls = {u for _, u in stored}
            by_title = sum(normalize_text(r["title"]) in titles for r in rows)
            by_url = sum(normalize_url(r["url"]) in urls for r in rows)
            result.append(
                {
                    "ticker": window["ticker"],
                    "age_months": window["age_months"],
                    "probe_relevant": len(rows),
                    "stored_in_window": len(stored),
                    "overlap_title": by_title,
                    "overlap_url": by_url,
                }
            )
    finally:
        connection.close()
    return result


def gkg_sample(client: Client, stamp: str) -> dict:
    """Fetch one 15-minute GKG file into memory (capped) and describe it; nothing is stored."""
    import io
    import zipfile

    url = f"http://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip"
    cap = 30_000_000
    wait = client.interval - (time.monotonic() - client._last)
    if client._last and wait > 0:
        time.sleep(wait)
    client.requests += 1
    out: dict = {"url": url}
    with httpx.stream(
        "GET", url, headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True
    ) as response:
        out["status"] = response.status_code
        if response.status_code in (403, 429):
            raise Stop(f"HTTP {response.status_code}")
        if response.status_code != 200:
            return out
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > cap:
                out["aborted"] = "over 30 MB cap"
                return out
    out["zip_bytes"] = len(body)
    with zipfile.ZipFile(io.BytesIO(bytes(body))) as archive:
        name = archive.namelist()[0]
        text = archive.read(name).decode("utf-8", errors="replace")
    lines = [line for line in text.split(NL) if line]
    cols = [line.split(TAB) for line in lines]
    out.update(member=name, unzipped_bytes=len(text.encode()), rows=len(lines))
    out["column_counts"] = dict(Counter(len(c) for c in cols))
    width = Counter(len(c) for c in cols).most_common(1)[0][0]
    out["columns_modal_width"] = width
    sample = [c for c in cols if len(c) == width]
    out["first_row_fields_truncated"] = [f[:60] for f in sample[0]]
    out["rows_with_url"] = sum(bool(c[4].startswith("http")) for c in sample)
    out["rows_with_page_title"] = sum("<PAGE_TITLE>" in c[-1] for c in sample)
    out["rows_with_precise_pubtime"] = sum("PAGE_PRECISEPUBTIMESTAMP" in c[-1] for c in sample)
    out["rows_with_organizations"] = sum(bool(c[13] or c[14]) for c in sample)
    out["distinct_domains"] = len({c[3] for c in sample})
    low = [(" ".join(c[11:15] + [c[-1]])).lower() for c in sample]
    out["rows_mentioning"] = {
        name: sum(name in row for row in low)
        for name in ("nvidia", "pfizer", "astrazeneca", "apple", "microsoft")
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "mode",
        choices=[
            "gdelt-diag",
            "gdelt-windows",
            "google-windows",
            "google-shorter",
            "gkg-sample",
            "overlap",
            "summarize",
        ],
    )
    parser.add_argument("--tickers", default="NVDA,PFE,AZN")
    parser.add_argument("--ages", default=None, help="comma-separated months")
    parser.add_argument("--age", type=int, default=24, help="google-shorter age in months")
    parser.add_argument("--out", type=Path, help="write/read probe rows (JSON)")
    parser.add_argument("--now", default=None, help="ISO timestamp; default current UTC time")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
    tickers = args.tickers.split(",")
    ages = [int(a) for a in args.ages.split(",")] if args.ages else None
    client = Client(GDELT_INTERVAL if args.mode.startswith("gdelt") else GOOGLE_INTERVAL)

    try:
        if args.mode == "gdelt-diag":
            payload = gdelt_diag(client, now)
        elif args.mode == "gdelt-windows":
            payload = gdelt_windows(client, now, tickers, ages or [3, 6, 12, 24, 36])
        elif args.mode == "google-windows":
            payload = google_windows(client, now, tickers, ages or [13, 18, 24, 36])
        elif args.mode == "gkg-sample":
            payload = gkg_sample(client, "20230115120000")
            print(json.dumps(payload, indent=1))
        elif args.mode == "google-shorter":
            payload = google_shorter(client, now, tickers[0], args.age)
        else:
            payload = json.loads(args.out.read_text(encoding="utf-8"))
            if args.mode == "overlap":
                print(json.dumps(overlap(payload), indent=1))
            else:
                print(json.dumps(summarize(payload), indent=1))
            return 0
    except Stop as stop:
        print(f"STOPPED on block/rate-limit signal after {client.requests} requests: {stop}")
        return 2
    payload["network_requests"] = client.requests
    payload["now"] = now.isoformat()
    if args.out:
        args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    if "windows" in payload:
        print(json.dumps(summarize(payload), indent=1))
    print(f"network requests this run: {client.requests}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
