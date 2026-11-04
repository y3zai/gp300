-- Keep the append-only history as the source of truth. Store the three
-- published resolutions per UTC day so the API can read whole days directly.
CREATE VIEW IF NOT EXISTS history_points AS
SELECT substr(timestamp, 1, 10) AS day, timestamp,
       json_object('timestamp', timestamp, 'value', value,
                   'num_constituents', num_constituents,
                   'weighted_entropy', weighted_entropy, 'divisor', divisor) AS point
FROM history;

CREATE TABLE IF NOT EXISTS history_payloads (
    day TEXT PRIMARY KEY,
    recent TEXT NOT NULL,
    hourly TEXT NOT NULL,
    daily TEXT NOT NULL
) WITHOUT ROWID;

INSERT OR REPLACE INTO history_payloads
SELECT day,
       json_group_array(json(point) ORDER BY timestamp) AS recent,
       json_group_array(json(point) ORDER BY timestamp) FILTER (WHERE hour_rank = 1) AS hourly,
       json_group_array(json(point)) FILTER (WHERE day_rank = 1) AS daily
FROM (
    SELECT *,
           row_number() OVER (PARTITION BY day, substr(timestamp, 1, 13)
                              ORDER BY timestamp DESC) AS hour_rank,
           row_number() OVER (PARTITION BY day ORDER BY timestamp DESC) AS day_rank
    FROM history_points
)
GROUP BY day;

CREATE TRIGGER IF NOT EXISTS history_refresh_payload_after_insert
AFTER INSERT ON history
BEGIN
    -- Filter by the indexed timestamp before ranking. A filter on the
    -- outside of the window query can scan the entire history on each insert.
    INSERT OR REPLACE INTO history_payloads
    SELECT day,
           json_group_array(json(point) ORDER BY timestamp),
           json_group_array(json(point) ORDER BY timestamp) FILTER (WHERE hour_rank = 1),
           json_group_array(json(point)) FILTER (WHERE day_rank = 1)
    FROM (
        SELECT *,
               row_number() OVER (PARTITION BY substr(timestamp, 1, 13)
                                  ORDER BY timestamp DESC) AS hour_rank,
               row_number() OVER (ORDER BY timestamp DESC) AS day_rank
        FROM history_points
        WHERE timestamp >= substr(NEW.timestamp, 1, 10)
          AND timestamp < date(substr(NEW.timestamp, 1, 10), '+1 day')
    )
    GROUP BY day;
END;
