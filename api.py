"""
Polymarket API client for G&P 300.

Three APIs:
- Gamma (gamma-api.polymarket.com): Market discovery, metadata, volume stats
- CLOB (clob.polymarket.com): Live prices, historical price data
- Data (data-api.polymarket.com): User positions (not needed for index)

All read endpoints require NO authentication.
"""

import json
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
GEOPOLITICS_TAG_ID = 100265


@dataclass
class Market:
    """A single binary market (Yes/No contract)."""
    id: str
    question: str
    slug: str
    condition_id: str
    event_id: str

    # Outcomes & prices
    outcomes: list[str]           # ["Yes", "No"]
    outcome_prices: list[float]   # [0.62, 0.38]
    clob_token_ids: list[str]     # token IDs for CLOB API

    # Volume & liquidity
    volume_total: float
    volume_24h: float
    volume_1wk: float
    volume_1mo: float
    volume_1yr: float
    liquidity: float

    # Dates
    end_date: Optional[datetime]
    start_date: Optional[datetime]

    # Status
    active: bool
    closed: bool
    neg_risk: bool

    # Metadata
    group_item_title: Optional[str]  # e.g., "February 5", "March 31"
    last_trade_price: Optional[float]
    one_day_price_change: Optional[float]


@dataclass
class Event:
    """A Polymarket event containing one or more markets."""
    id: str
    title: str
    slug: str

    # Volume at event level
    volume_total: float
    volume_24h: float
    volume_1mo: float
    liquidity: float

    # Dates
    end_date: Optional[datetime]
    start_date: Optional[datetime]

    # Status
    active: bool
    closed: bool
    neg_risk: bool

    # Child markets
    markets: list[Market] = field(default_factory=list)


def _parse_datetime(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 datetime string."""
    if not s:
        return None
    try:
        # Handle various formats
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _parse_stringified_json(s: Optional[str]) -> list:
    """Parse stringified JSON arrays from Polymarket API.

    Fields like outcomes, outcomePrices, clobTokenIds come as
    stringified JSON: '["Yes", "No"]' instead of ["Yes", "No"].
    """
    if not s:
        return []
    if isinstance(s, list):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return []


def _safe_float(v, default=0.0) -> float:
    """Safely convert to float."""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _parse_market(m: dict) -> Market:
    """Parse a market dict from the Gamma API into a Market object."""
    outcome_prices_raw = _parse_stringified_json(m.get("outcomePrices"))
    outcome_prices = [_safe_float(p) for p in outcome_prices_raw]

    return Market(
        id=str(m.get("id", "")),
        question=m.get("question", ""),
        slug=m.get("slug", ""),
        condition_id=m.get("conditionId", ""),
        event_id=str(m.get("eventId", m.get("event_id", ""))),
        outcomes=_parse_stringified_json(m.get("outcomes")),
        outcome_prices=outcome_prices,
        clob_token_ids=_parse_stringified_json(m.get("clobTokenIds")),
        volume_total=_safe_float(m.get("volumeNum", m.get("volume"))),
        volume_24h=_safe_float(m.get("volume24hr")),
        volume_1wk=_safe_float(m.get("volume1wk")),
        volume_1mo=_safe_float(m.get("volume1mo")),
        volume_1yr=_safe_float(m.get("volume1yr")),
        liquidity=_safe_float(m.get("liquidityNum", m.get("liquidity"))),
        end_date=_parse_datetime(m.get("endDate")),
        start_date=_parse_datetime(m.get("startDate")),
        active=bool(m.get("active", False)),
        closed=bool(m.get("closed", False)),
        neg_risk=bool(m.get("negRisk", False)),
        group_item_title=m.get("groupItemTitle"),
        last_trade_price=_safe_float(m.get("lastTradePrice")) if m.get("lastTradePrice") else None,
        one_day_price_change=_safe_float(m.get("oneDayPriceChange")) if m.get("oneDayPriceChange") else None,
    )


def _parse_event(e: dict) -> Event:
    """Parse an event dict from the Gamma API into an Event object."""
    markets = [_parse_market(m) for m in e.get("markets", [])]

    return Event(
        id=str(e.get("id", "")),
        title=e.get("title", ""),
        slug=e.get("slug", ""),
        volume_total=_safe_float(e.get("volume")),
        volume_24h=_safe_float(e.get("volume24hr")),
        volume_1mo=_safe_float(e.get("volume1mo")),
        liquidity=_safe_float(e.get("liquidityClob", e.get("liquidity"))),
        end_date=_parse_datetime(e.get("endDate")),
        start_date=_parse_datetime(e.get("startDate")),
        active=bool(e.get("active", False)),
        closed=bool(e.get("closed", False)),
        neg_risk=bool(e.get("negRisk", False)),
        markets=markets,
    )


def fetch_geopolitics_events(
    closed: bool = False,
    limit: int = 100,
    max_pages: int = 20,
    delay: float = 0.1,
) -> list[Event]:
    """
    Fetch all geopolitics events from the Gamma API.

    Handles pagination automatically.

    Args:
        closed: If False, only fetch open events
        limit: Results per page (max 100)
        max_pages: Safety limit on pagination
        delay: Delay between requests in seconds

    Returns:
        List of Event objects with nested Markets
    """
    all_events = []
    offset = 0

    for page in range(max_pages):
        params = {
            "tag_id": GEOPOLITICS_TAG_ID,
            "related_tags": "true",
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
        }

        resp = requests.get(f"{GAMMA_BASE}/events", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            break

        for e in data:
            all_events.append(_parse_event(e))

        if len(data) < limit:
            break

        offset += limit
        if delay > 0:
            time.sleep(delay)

    return all_events


def fetch_market_prices(clob_token_id: str) -> Optional[float]:
    """Fetch the current price for a single CLOB token."""
    try:
        resp = requests.get(
            f"{CLOB_BASE}/price",
            params={"token_id": clob_token_id, "side": "buy"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return _safe_float(data.get("price"))
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None


def fetch_price_history(
    clob_token_id: str,
    interval: str = "max",
    fidelity: int = 60,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> list[dict]:
    """
    Fetch historical prices for a CLOB token.

    Args:
        clob_token_id: The CLOB token ID
        interval: "1h", "6h", "1d", "1w", "1m", "max"
        fidelity: Resolution in minutes (min 5)
        start_ts: Unix timestamp start (mutually exclusive with interval)
        end_ts: Unix timestamp end

    Returns:
        List of {"t": unix_timestamp, "p": price} dicts
    """
    params = {"market": clob_token_id, "fidelity": max(fidelity, 5)}

    if start_ts is not None and end_ts is not None:
        params["startTs"] = start_ts
        params["endTs"] = end_ts
    else:
        params["interval"] = interval

    try:
        resp = requests.get(f"{CLOB_BASE}/prices-history", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("history", [])
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return []
