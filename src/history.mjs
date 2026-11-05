const DAY_MS = 86_400_000;
const DAY_MICROSECONDS = DAY_MS * 1_000;
const MIDNIGHT = "T00:00:00+00:00";
const EARLIEST = "0001-01-01" + MIDNIGHT;
const INVALID_SINCE = "since must be an ISO 8601 timestamp";
const ISO_DATE_TIME = /^(\d{4}(?:-\d{2}-\d{2}|\d{4}|-W\d{2}(?:-\d)?|W\d{2}\d?))(?:.(.*))?$/su;
const ISO_CLOCK = /^(\d{2})(?:(:?)(\d{2})(?:\2(\d{2})(?:[.,](\d+))?)?)?$/;

function calendarDay(value) {
  if (value.startsWith("0000")) return NaN;

  const weekDate = /^(\d{4})-?W(\d{2})(?:-?(\d))?$/.exec(value);
  if (weekDate) {
    const [, year, week, weekday = "1"] = weekDate;
    if (+week < 1 || +week > 53 || +weekday < 1 || +weekday > 7) return NaN;
    const januaryFourth = new Date(year + "-01-04T00:00:00Z");
    const daysSinceMonday = (januaryFourth.getUTCDay() + 6) % 7;
    const monday = +januaryFourth + (7 * (+week - 1) - daysSinceMonday) * DAY_MS;
    const thursday = new Date(monday + 3 * DAY_MS);
    return thursday.getUTCFullYear() === +year ? monday + (+weekday - 1) * DAY_MS : NaN;
  }

  const day = value.length === 8 ? value.replace(/(\d{4})(\d{2})(\d{2})/, "$1-$2-$3") : value;
  const date = new Date(day + "T00:00:00Z");
  return Number.isFinite(+date) && date.toISOString().startsWith(day) ? +date : NaN;
}

function clockParts(value) {
  const match = ISO_CLOCK.exec(value);
  if (!match) return null;
  const [, hours, , minutes = "00", seconds = "00", fraction = ""] = match;
  return [+hours, +minutes, +seconds, +fraction.slice(0, 6).padEnd(6, "0")];
}

function clockMicroseconds([hours, minutes, seconds, fraction]) {
  return ((hours * 60 + minutes) * 60 + seconds) * 1_000_000 + fraction;
}

function utcTimestamp(ms, submillisecond = 0) {
  const iso = new Date(ms).toISOString();
  const fraction = iso.slice(20, 23) + String(submillisecond).padStart(3, "0");
  return iso.slice(0, 19) + (fraction === "000000" ? "" : "." + fraction) + "+00:00";
}

function parseSince(since) {
  if (since == null) return EARLIEST;
  const match = typeof since === "string" && ISO_DATE_TIME.exec(since.replaceAll("Z", "+00:00"));
  if (!match) throw new RangeError(INVALID_SINCE);
  const [, day, time] = match;
  const midnight = calendarDay(day);
  if (!Number.isFinite(midnight)) throw new RangeError(INVALID_SINCE);
  if (time === undefined) throw new RangeError("since must include a timezone");

  const sign = time.search(/[+-]/);
  const clock = clockParts(sign === -1 ? time : time.slice(0, sign));
  if (!clock || clock[0] > 23 || clock[1] > 59 || clock[2] > 59) throw new RangeError(INVALID_SINCE);
  if (sign === -1) throw new RangeError("since must include a timezone");
  const offset = clockParts(time.slice(sign + 1));
  if (!offset || clockMicroseconds(offset) >= DAY_MICROSECONDS) throw new RangeError(INVALID_SINCE);

  const direction = time[sign] === "-" ? -1 : 1;
  const utcMicroseconds = clockMicroseconds(clock) - direction * clockMicroseconds(offset);
  // Do offset arithmetic relative to midnight to keep full microsecond precision.
  const milliseconds = Math.floor(utcMicroseconds / 1_000);
  const utc = midnight + milliseconds;
  if (utc < Date.parse("0001-01-01") || utc > Date.parse("9999-12-31T23:59:59.999Z")) {
    throw new RangeError(INVALID_SINCE);
  }
  return utcTimestamp(utc, utcMicroseconds - milliseconds * 1_000);
}

export function buildHistoryPayloadQuery(since, now = new Date()) {
  const lower = parseSince(since);
  const hourlyCutoff = utcTimestamp(+now - 365 * DAY_MS);
  const recentCutoff = utcTimestamp(+now - 7 * DAY_MS);
  const days = [];
  const partials = [];

  // UTC ISO timestamps have the same lexical ordering as the stored D1 timestamps.
  for (const [resolution, start, end] of [
    ["daily", lower, hourlyCutoff],
    ["hourly", lower > hourlyCutoff ? lower : hourlyCutoff, recentCutoff],
    ["recent", lower > recentCutoff ? lower : recentCutoff, "~"],
  ]) {
    if (start >= end) continue;
    const endDay = end.slice(0, 10);
    let firstDay = start.slice(0, 10);
    let startTimestamp = start;

    if (!start.endsWith(MIDNIGHT)) {
      firstDay = firstDay === "9999-12-31" ? "~" : new Date(Date.parse(firstDay) + DAY_MS).toISOString().slice(0, 10);
      const nextMidnight = firstDay === "~" ? "~" : firstDay + MIDNIGHT;
      const headEnd = end < nextMidnight ? end : nextMidnight;
      partials.push([resolution, startTimestamp, headEnd]);
      startTimestamp = headEnd;
    }

    if (firstDay < endDay) {
      days.push([resolution, firstDay, endDay]);
      startTimestamp = endDay + MIDNIGHT;
    }

    if (end !== "~" && startTimestamp < end) {
      partials.push([resolution, startTimestamp, end]);
    }
  }

  // Dummy ranges keep both VALUES clauses valid when one set is empty.
  if (!days.length) days.push(["", "", ""]);
  if (!partials.length) partials.push(["", "", ""]);
  const dayValues = days.map(() => "(?, ?, ?)").join(", ");
  const partialValues = partials.map(() => "(?, ?, ?)").join(", ");
  const sql = SQL.replace("{day_values}", dayValues).replace("{partial_values}", partialValues);
  return [sql, days.concat(partials).flat()];
}

const SQL = `
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
    `;
