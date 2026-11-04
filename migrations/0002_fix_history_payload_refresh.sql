-- The history writer uses INSERT OR IGNORE. An outer conflict policy
-- overrides OR REPLACE in a trigger, so use a targeted UPSERT instead.
DROP TRIGGER IF EXISTS history_refresh_payload_after_insert;

CREATE TRIGGER history_refresh_payload_after_insert
AFTER INSERT ON history
BEGIN
    INSERT INTO history_payloads (day, recent, hourly, daily)
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
    GROUP BY day
    ON CONFLICT(day) DO UPDATE SET
        recent = excluded.recent, hourly = excluded.hourly, daily = excluded.daily;
END;

-- Repair daily payloads that missed inserts under the original trigger.
INSERT OR REPLACE INTO history_payloads
SELECT day,
       json_group_array(json(point) ORDER BY timestamp),
       json_group_array(json(point) ORDER BY timestamp) FILTER (WHERE hour_rank = 1),
       json_group_array(json(point)) FILTER (WHERE day_rank = 1)
FROM (
    SELECT *,
           row_number() OVER (PARTITION BY day, substr(timestamp, 1, 13)
                              ORDER BY timestamp DESC) AS hour_rank,
           row_number() OVER (PARTITION BY day ORDER BY timestamp DESC) AS day_rank
    FROM history_points
)
GROUP BY day;
