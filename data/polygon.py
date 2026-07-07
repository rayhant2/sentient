from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config.settings import settings
from data import database
from models.schemas import OHLCVPoint, Ticker


POLYGON_BASE_URL = "https://api.polygon.io"
DEFAULT_MULTIPLIER = 15
DEFAULT_TIMESPAN = "minute"
DEFAULT_LOOKBACK_DAYS = 10


class PolygonError(RuntimeError):
    """Raised when Polygon data cannot be fetched or parsed."""


class MissingPolygonConfigError(PolygonError):
    """Raised when POLYGON_API_KEY is missing."""


class PolygonAPIError(PolygonError):
    """Raised when Polygon returns an error response."""


def get_polygon_api_key() -> str:
    if settings.polygon_api_key is None:
        raise MissingPolygonConfigError("POLYGON_API_KEY must be set before fetching prices.")
    return settings.polygon_api_key.get_secret_value()


def _ticker_symbol(ticker: str) -> str:
    return ticker.upper()


def _format_polygon_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).date().isoformat()


def _bar_to_point(ticker: str, bar: dict[str, Any]) -> OHLCVPoint:
    try:
        timestamp_ms = bar["t"]
        return OHLCVPoint(
            ticker=_ticker_symbol(ticker),
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
            open=bar["o"],
            high=bar["h"],
            low=bar["l"],
            close=bar["c"],
            volume=bar["v"],
        )
    except KeyError as exc:
        raise PolygonError(f"Polygon bar is missing expected field: {exc.args[0]}") from exc


def parse_ohlcv_points(ticker: str, payload: dict[str, Any], limit: int) -> list[OHLCVPoint]:
    results = payload.get("results") or []
    if not isinstance(results, list):
        raise PolygonError("Polygon response field 'results' must be a list.")

    points = [_bar_to_point(ticker, bar) for bar in results]
    points.sort(key=lambda point: point.timestamp)
    return points[-limit:]


def _build_aggs_url(
    ticker: str,
    *,
    from_date: datetime,
    to_date: datetime,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    limit: int = settings.max_ticker_datapoints,
) -> str:
    encoded_ticker = _ticker_symbol(ticker)
    path = (
        f"/v2/aggs/ticker/{encoded_ticker}/range/"
        f"{multiplier}/{timespan}/"
        f"{_format_polygon_date(from_date)}/{_format_polygon_date(to_date)}"
    )
    query = urlencode(
        {
            "adjusted": "true",
            "sort": "asc",
            "limit": limit,
            "apiKey": get_polygon_api_key(),
        }
    )
    return f"{POLYGON_BASE_URL}{path}?{query}"


def _get_json(url: str, *, timeout: int = 15) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise PolygonAPIError(f"Polygon request failed with status {exc.code}: {body}") from exc
    except URLError as exc:
        raise PolygonAPIError(f"Polygon request failed: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PolygonError("Polygon returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise PolygonError("Polygon response must be a JSON object.")
    return payload


def fetch_intraday_ohlcv(
    ticker: str,
    limit: int = settings.max_ticker_datapoints,
    *,
    to_date: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
) -> list[OHLCVPoint]:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be greater than zero")

    end = to_date or datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    url = _build_aggs_url(
        ticker,
        from_date=start,
        to_date=end,
        multiplier=multiplier,
        timespan=timespan,
        limit=limit,
    )
    payload = _get_json(url)

    status = payload.get("status")
    if status not in {None, "OK", "DELAYED"}:
        message = payload.get("error") or payload.get("message") or status
        raise PolygonAPIError(f"Polygon returned status {status}: {message}")

    return parse_ohlcv_points(ticker, payload, limit)


def refresh_ticker_data(
    ticker: str,
    limit: int = settings.max_ticker_datapoints,
    *,
    next_fetch_minutes: int = settings.price_fetch_interval_minutes,
) -> list[OHLCVPoint]:
    points = fetch_intraday_ohlcv(ticker, limit=limit)
    if not points:
        return []

    now = datetime.now(timezone.utc)
    latest = points[-1]
    database.upsert_ticker(
        Ticker(
            ticker=_ticker_symbol(ticker),
            last_fetched=now,
            next_fetch_time=now + timedelta(minutes=next_fetch_minutes),
            current_price=latest.close,
        )
    )

    for point in points:
        database.insert_ticker_data(point)

    database.delete_old_ticker_data(_ticker_symbol(ticker), keep=limit)
    return points
