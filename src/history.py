from datetime import date, datetime, time, timedelta, timezone


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


def build_history_payload_query(*, since=None, now=None):
    """Read pre-serialized whole days and sample only the partial UTC days."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lower = _parse_since(since) or datetime.min.replace(tzinfo=timezone.utc)
    hourly_cutoff = now - timedelta(days=HOURLY_DETAIL_DAYS)
    recent_cutoff = now - timedelta(days=RECENT_DETAIL_DAYS)
    days = []
    partials = []

    for resolution, start, end in (
        ("daily", lower, hourly_cutoff),
        ("hourly", max(lower, hourly_cutoff), recent_cutoff),
        ("recent", max(lower, recent_cutoff), None),
    ):
        if end is not None and start >= end:
            continue
        end_timestamp = end.isoformat() if end is not None else "~"
        end_day = end.date().isoformat() if end is not None else "~"
        start_timestamp = start.isoformat()
        first_day = start.date().isoformat()

        if start.time() != time.min:
            first_day = (start.date() + timedelta(days=1)).isoformat() if start.date() < date.max else "~"
            start_of_next_day = first_day if first_day == "~" else first_day + "T00:00:00+00:00"
            head_end = min(end_timestamp, start_of_next_day)
            partials.append((resolution, start_timestamp, head_end))
            start_timestamp = head_end

        if first_day < end_day:
            days.append((resolution, first_day, end_day))
            start_timestamp = end_day + "T00:00:00+00:00"

        if end is not None and start_timestamp < end_timestamp:
            partials.append((resolution, start_timestamp, end_timestamp))

    # Dummy ranges keep both VALUES clauses valid when one set is empty.
    days = days or [("", "", "")]
    partials = partials or [("", "", "")]
    day_values = ", ".join(["(?, ?, ?)"] * len(days))
    partial_values = ", ".join(["(?, ?, ?)"] * len(partials))
    sql = f"""
        WITH
        days(resolution, first_day, end_day) AS (VALUES {day_values}),
        partials(resolution, start, end) AS (VALUES {partial_values}),
        boundary_points AS (
            SELECT p.resolution, h.day, h.timestamp, h.point,
                   row_number() OVER (
                       PARTITION BY p.resolution,
                           CASE p.resolution
                               WHEN 'daily' THEN h.day
                               WHEN 'hourly' THEN substr(h.timestamp, 1, 13)
                               ELSE h.timestamp
                           END
                       ORDER BY h.timestamp DESC
                   ) AS rank
            FROM partials AS p
            CROSS JOIN history_points AS h
            WHERE h.timestamp >= p.start AND h.timestamp < p.end
        ),
        chunks AS (
            SELECT h.day AS first_timestamp,
                   CASE d.resolution
                       WHEN 'daily' THEN h.daily
                       WHEN 'hourly' THEN h.hourly
                       ELSE h.recent
                   END AS payload
            FROM days AS d
            CROSS JOIN history_payloads AS h
            WHERE h.day >= d.first_day AND h.day < d.end_day
            UNION ALL
            SELECT min(timestamp), json_group_array(json(point) ORDER BY timestamp)
            FROM boundary_points
            WHERE rank = 1
            GROUP BY resolution, day
        )
        SELECT payload FROM chunks ORDER BY first_timestamp
    """
    return sql, [value for row in days + partials for value in row]
