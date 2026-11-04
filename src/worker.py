"""
GP300 Cloudflare Worker.

Cron triggers run the index pipeline at different intervals.
HTTP API serves current data from D1.
"""

import json
import traceback
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from js import console, fetch as js_fetch
from workers import WorkerEntrypoint, Response

from api import (
    GAMMA_BASE,
    GEOPOLITICS_TAG_ID,
    _parse_event,
)
from d1_state import STATE_QUERY, build_d1_update, decode_state, select_update
from engine import run_full_pipeline
from history import build_history_payload_query, build_history_query


# ── Async HTTP helpers ───────────────────────────


async def _fetch_json(url, params=None):
    """Fetch JSON from an external URL using Workers' built-in fetch."""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    resp = await js_fetch(url)
    if not resp.ok:
        raise RuntimeError(f"fetch {url} → {resp.status}")
    data = await resp.json()
    return data.to_py()


async def fetch_events_async():
    """Fetch all geopolitics events from Polymarket (async, paginated)."""
    all_events = []
    offset = 0
    for _ in range(20):
        params = {
            "tag_id": GEOPOLITICS_TAG_ID,
            "related_tags": "true",
            "closed": "false",
            "active": "true",
            "limit": 100,
            "offset": offset,
            "order": "volume",
            "ascending": "false",
        }
        data = await _fetch_json(f"{GAMMA_BASE}/events", params)
        if not data:
            break
        for e in data:
            all_events.append(_parse_event(e))
        if len(data) < 100:
            break
        offset += 100
    return all_events


# ── Helpers ──────────────────────────────────────


async def load_state_d1(db):
    """Read the engine snapshot and publication metadata from D1."""
    row = await db.prepare(STATE_QUERY).first()
    return decode_state(row.data if row else None)


async def save_results_d1(db, state, previous, scheduled_at, mode):
    """Publish only if D1 still has the engine state read by this invocation."""
    statements = build_d1_update(state, previous, scheduled_at, mode)
    result = await db.batch([db.prepare(sql).bind(*params) for sql, params in statements])
    return bool(result[len(statements) - 1].meta.changes)


# ── CORS ─────────────────────────────────────────

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def json_text_response(body, status=200):
    return Response(
        body,
        status=status,
        headers={"Content-Type": "application/json", **CORS_HEADERS},
    )


def json_response(data, status=200):
    return json_text_response(json.dumps(data), status)


# ── Worker entry point ───────────────────────────


class Default(WorkerEntrypoint):
    # ── HTTP API ─────────────────────────────────

    async def fetch(self, request):
        if request.method == "OPTIONS":
            return Response("", headers=CORS_HEADERS)

        path = urlparse(request.url).path.rstrip("/")
        params = parse_qs(urlparse(request.url).query)

        if path == "/api/current":
            return await self._api_current()
        if path == "/api/constituents":
            return await self._api_constituents()
        if path == "/api/history":
            since = params.get("since", [None])[0]
            return await self._api_history(since)
        # Unknown API path
        return json_response({"error": "not found"}, status=404)

    async def _get_cached_snapshot(self):
        """Read the unified KV snapshot; return parsed dict or None if missing/stale/corrupt."""
        try:
            raw = await self.env.CACHE_GP300.get("snapshot")
            if not raw:
                return None
            snapshot = json.loads(raw)
            cached_at = datetime.fromisoformat(snapshot["cached_at"])
            age = (datetime.now(timezone.utc) - cached_at).total_seconds()
            if age > 600:  # 10 minutes — 2× the 5-min cron interval
                return None
            return snapshot
        except Exception as e:
            console.error(f"[gp300] KV snapshot read/parse failed, falling back to D1: {e}")
            return None

    async def _api_current(self):
        snapshot = await self._get_cached_snapshot()
        if snapshot:
            return json_response(snapshot["current"])
        row = await self.env.DB.prepare(
            "SELECT index_value, timestamp, num_constituents, "
            "weighted_entropy, divisor FROM current WHERE id = 1"
        ).first()
        if not row:
            return json_response({"error": "no data"}, status=404)
        # Read last_rebase from state blob
        last_rebase = None
        state_row = await self.env.DB.prepare("SELECT data FROM state WHERE id = 1").first()
        if state_row:
            try:
                last_rebase = json.loads(state_row.data).get("last_rebase")
            except Exception:
                pass
        return json_response(
            {
                "index_value": row.index_value,
                "timestamp": row.timestamp,
                "num_constituents": row.num_constituents,
                "weighted_entropy": row.weighted_entropy,
                "divisor": row.divisor,
                "last_rebase": last_rebase,
            }
        )

    async def _api_constituents(self):
        snapshot = await self._get_cached_snapshot()
        if snapshot:
            return json_response(snapshot["constituents"])
        result = await self.env.DB.prepare(
            "SELECT id, label, source_type, num_outcomes, probabilities, "
            "normalized_entropy, weight, volume_1mo, end_date, rank "
            "FROM constituents ORDER BY weight DESC"
        ).all()
        rows = result.results
        if hasattr(rows, "to_py"):
            rows = rows.to_py()
        return json_response(
            [
                {
                    "id": r["id"],
                    "label": r["label"],
                    "source_type": r["source_type"],
                    "num_outcomes": r["num_outcomes"],
                    "probabilities": json.loads(r["probabilities"]),
                    "normalized_entropy": r["normalized_entropy"],
                    "weight": r["weight"],
                    "volume_1mo": r["volume_1mo"],
                    "end_date": r["end_date"],
                    "rank": r["rank"],
                }
                for r in rows
            ]
        )

    async def _api_history(self, since=None):
        try:
            query, values = build_history_payload_query(since=since)
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)
        db = self.env.DB
        try:
            result = await db.prepare(query).bind(*values).all()
        except Exception as error:
            # Support deployments where the D1 migration has not run yet.
            if not any(
                f"no such table: {table}" in str(error)
                for table in ("history_payloads", "history_points")
            ):
                raise
            query, values = build_history_query(since=since)
            result = await db.prepare(query).bind(*values).all()
            return json_response(result.results.to_py())

        chunks = result.results.to_py()
        return json_text_response("[" + ",".join(row["payload"][1:-1] for row in chunks) + "]")

    # ── Cron handler ─────────────────────────────

    async def scheduled(self, event, env, ctx):
        scheduled = datetime.fromtimestamp(event.scheduledTime / 1000, timezone.utc)
        if event.cron in ("1 0 * * SUN", "1 0 * * MON-SAT"):
            slot = scheduled.replace(hour=0, minute=1, second=0, microsecond=0)
        elif event.cron == "*/5 * * * *":
            slot = scheduled.replace(minute=scheduled.minute // 5 * 5, second=0, microsecond=0)
        else:
            console.error(f"[gp300] unexpected cron schedule: {event.cron!r} — skipping")
            return
        await self._run_pipeline(slot)

    async def _run_pipeline(self, slot):
        try:
            db = self.env.DB
            previous = await load_state_d1(db)
            now = datetime.now(timezone.utc)
            mode = select_update(previous, slot)
            if mode is None:
                console.log(f"[gp300] skipping completed slot {slot.isoformat()}")
                return

            events = await fetch_events_async()
            console.log(
                f"[gp300] fetched {len(events)} events, "
                f"reconstitute={mode.reconstitute}, rebalance={mode.rebalance}"
            )

            state = run_full_pipeline(
                events=events,
                current_state=previous.state if previous else None,
                now=now,
                reconstitute=mode.reconstitute,
                rebalance=mode.rebalance,
            )
            console.log(f"[gp300] pipeline done: value={state.value}, constituents={state.num_constituents}")

            if not await save_results_d1(db, state, previous, slot, mode):
                console.log("[gp300] state changed before publication")
                return
            console.log("[gp300] saved to D1")

            # Cache current + constituents as a single atomic snapshot in KV
            try:
                snapshot = json.dumps({
                    "cached_at": now.isoformat(),
                    "current": {
                        "index_value": round(state.value, 2),
                        "timestamp": state.timestamp.isoformat(),
                        "num_constituents": state.num_constituents,
                        "weighted_entropy": round(state.weighted_entropy, 6),
                        "divisor": state.divisor,
                        "last_rebase": state.last_rebase.isoformat() if state.last_rebase else None,
                    },
                    "constituents": [
                        {
                            "id": c.id,
                            "label": c.label,
                            "source_type": c.source_type,
                            "num_outcomes": c.num_outcomes,
                            "probabilities": [round(p, 4) for p in c.probabilities],
                            "normalized_entropy": round(c.normalized_entropy, 4),
                            "weight": round(c.weight, 6),
                            "volume_1mo": round(c.volume_1mo, 2),
                            "end_date": c.end_date.isoformat() if c.end_date else None,
                            "rank": c.rank,
                        }
                        for c in sorted(state.constituents, key=lambda x: x.weight, reverse=True)
                    ],
                })
                await self.env.CACHE_GP300.put("snapshot", snapshot)
                console.log("[gp300] cached snapshot in KV")
            except Exception as cache_err:
                console.error(f"[gp300] KV cache write failed: {cache_err}")
                try:
                    await self.env.CACHE_GP300.delete("snapshot")
                except Exception:
                    pass
        except Exception as e:
            console.error(f"[gp300] pipeline error: {e}\n{traceback.format_exc()}")
            raise
