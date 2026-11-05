import assert from "node:assert/strict";
import test from "node:test";
import { buildHistoryPayloadQuery } from "../src/history.mjs";

const now = new Date("2026-11-04T15:25:00.123Z");

test("splits UTC daily, hourly, and recent ranges at partial days", () => {
  const [sql, params] = buildHistoryPayloadQuery(undefined, now);
  assert.deepEqual(params, [
    "daily", "0001-01-01", "2025-11-04",
    "hourly", "2025-11-05", "2026-10-28",
    "recent", "2026-10-29", "~",
    "daily", "2025-11-04T00:00:00+00:00", "2025-11-04T15:25:00.123000+00:00",
    "hourly", "2025-11-04T15:25:00.123000+00:00", "2025-11-05T00:00:00+00:00",
    "hourly", "2026-10-28T00:00:00+00:00", "2026-10-28T15:25:00.123000+00:00",
    "recent", "2026-10-28T15:25:00.123000+00:00", "2026-10-29T00:00:00+00:00",
  ]);
  assert.equal(sql.match(/\?/g).length, params.length);
});

test("normalizes since offsets without rounding away microseconds", () => {
  const [, params] = buildHistoryPayloadQuery("2026-10-28T17:55:00.123456+02:30", now);
  assert.deepEqual(params, [
    "recent", "2026-10-29", "~",
    "recent", "2026-10-28T15:25:00.123456+00:00", "2026-10-29T00:00:00+00:00",
  ]);
  const [, isoWeek] = buildHistoryPayloadQuery("2026-W44-3 172500.1234567+020000.000001", now);
  assert.equal(isoWeek[4], "2026-10-28T15:25:00.123455+00:00");
});

test("keeps both SQL VALUES clauses valid at midnight or on the last UTC date", () => {
  const [, midnight] = buildHistoryPayloadQuery("2026-11-05T02:00:00+02:00", now);
  assert.deepEqual(midnight, ["recent", "2026-11-05", "~", "", "", ""]);
  const [, lastDay] = buildHistoryPayloadQuery("9999-12-31T12:34:56Z", now);
  assert.deepEqual(lastDay, ["", "", "", "recent", "9999-12-31T12:34:56+00:00", "~"]);
});

test("rejects invalid ISO timestamps and reports missing timezone separately", () => {
  for (const since of ["", "invalid", "2026-02-29T00:00:00Z", "2026-11-04T24:00:00Z", "2026-W54-2T00:00:00Z"]) {
    assert.throws(() => buildHistoryPayloadQuery(since, now), /since must be an ISO 8601 timestamp/);
  }
  for (const since of ["2026-11-04", "2026-11-04T15:25:00"]) {
    assert.throws(() => buildHistoryPayloadQuery(since, now), /since must include a timezone/);
  }
});
