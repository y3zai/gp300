from datetime import datetime, timedelta, timezone


HISTORY_COLUMNS = (
    "timestamp, value, num_constituents, weighted_entropy, divisor"
)
RECENT_DETAIL_DAYS = 7
HOURLY_DETAIL_DAYS = 365


def _parse_since(since):
    if since is None:
        return None
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("since must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("since must include a timezone")
    return parsed.astimezone(timezone.utc)


def build_history_query(*, since=None, now=None):
    """Build a bounded, multi-resolution history query and its bind values."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(timezone.utc)
    since_dt = _parse_since(since)

    recent_cutoff = now - timedelta(days=RECENT_DETAIL_DAYS)
    hourly_cutoff = now - timedelta(days=HOURLY_DETAIL_DAYS)
    recent_lower = max(recent_cutoff, since_dt) if since_dt else recent_cutoff
    hourly_lower = max(hourly_cutoff, since_dt) if since_dt else hourly_cutoff

    daily_since_clause = ""
    params = [
        recent_lower.isoformat(),
        hourly_lower.isoformat(),
        recent_cutoff.isoformat(),
        hourly_cutoff.isoformat(),
    ]
    if since_dt:
        daily_since_clause = "AND timestamp >= ?"
        params.append(since_dt.isoformat())

    sql = f"""
        WITH
        recent AS (
            SELECT {HISTORY_COLUMNS}
            FROM history
            WHERE timestamp >= ?
        ),
        hourly_timestamps AS (
            SELECT MAX(timestamp) AS timestamp
            FROM history
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY substr(timestamp, 1, 13)
        ),
        daily_timestamps AS (
            SELECT MAX(timestamp) AS timestamp
            FROM history
            WHERE timestamp < ? {daily_since_clause}
            GROUP BY substr(timestamp, 1, 10)
        ),
        sampled AS (
            SELECT {HISTORY_COLUMNS}
            FROM recent
            UNION ALL
            SELECT h.{HISTORY_COLUMNS.replace(", ", ", h.")}
            FROM history AS h
            INNER JOIN hourly_timestamps AS sampled_hour
                ON h.timestamp = sampled_hour.timestamp
            UNION ALL
            SELECT h.{HISTORY_COLUMNS.replace(", ", ", h.")}
            FROM history AS h
            INNER JOIN daily_timestamps AS sampled_day
                ON h.timestamp = sampled_day.timestamp
        )
        SELECT {HISTORY_COLUMNS}
        FROM sampled
        ORDER BY timestamp
    """
    return sql, params
