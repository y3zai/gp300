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

# D1 bind() doesn't accept Python None (becomes JS undefined).
# We need JS null. JSON.parse("null") gives us exactly that.
from js import JSON as _JS_JSON
JS_NULL = _JS_JSON.parse("null")

from api import (
    GAMMA_BASE,
    GEOPOLITICS_TAG_ID,
    _parse_event,
)
from engine import (
    Constituent,
    IndexState,
    run_full_pipeline,
)
from history import build_history_query


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


def _make_constituent_snapshot(constituents):
    """Return a compact list of constituent dicts for adjustment logs."""
    return [
        {
            "id": c.id,
            "label": c.label,
            "source_type": c.source_type,
            "weight": round(c.weight, 6),
            "rank": c.rank,
            "normalized_entropy": round(c.normalized_entropy, 4),
            "volume_1mo": round(c.volume_1mo, 2),
        }
        for c in sorted(constituents, key=lambda x: x.weight, reverse=True)
    ]


# ── D1 helpers ───────────────────────────────────


async def load_state_d1(db):
    """Load engine state from D1. Returns IndexState or None."""
    row = await db.prepare("SELECT data FROM state WHERE id = 1").first()
    if not row:
        return None

    data = json.loads(row.data)
    constituents = []
    for cd in data.get("constituents", []):
        end_date = None
        if cd.get("end_date"):
            try:
                end_date = datetime.fromisoformat(cd["end_date"])
            except (ValueError, TypeError):
                pass
        constituents.append(
            Constituent(
                id=cd["id"],
                label=cd["label"],
                source_type=cd["source_type"],
                num_outcomes=cd["num_outcomes"],
                probabilities=cd["probabilities"],
                volume_1mo=cd["volume_1mo"],
                end_date=end_date,
                weight=cd.get("weight", 0.0),
                rank=cd.get("rank", 0),
                normalized_entropy=cd.get("normalized_entropy", 0.0),
                source_market_ids=cd.get("source_market_ids", []),
            )
        )

    ts = datetime.fromisoformat(data["timestamp"])
    last_rebase = None
    if data.get("last_rebase"):
        try:
            last_rebase = datetime.fromisoformat(data["last_rebase"])
        except (ValueError, TypeError):
            pass
    return IndexState(
        value=data["value"],
        divisor=data["divisor"],
        weighted_entropy=data["weighted_entropy"],
        num_constituents=data["num_constituents"],
        timestamp=ts,
        constituents=constituents,
        last_rebase=last_rebase,
    )


async def save_results_d1(
    db, state, *, prev_state=None, reconstitute=False, rebalance=False, is_first_run=False
):
    """Write current, constituents, history, state, and adjustments to D1 in a single batch."""
    stmts = []

    # current
    stmts.append(
        db.prepare(
            "INSERT OR REPLACE INTO current "
            "(id, index_value, timestamp, num_constituents, weighted_entropy, divisor) "
            "VALUES (1, ?, ?, ?, ?, ?)"
        ).bind(
            round(state.value, 2),
            state.timestamp.isoformat(),
            state.num_constituents,
            round(state.weighted_entropy, 6),
            state.divisor,
        )
    )

    # constituents — delete-then-insert
    stmts.append(db.prepare("DELETE FROM constituents"))
    for c in sorted(state.constituents, key=lambda x: x.weight, reverse=True):
        stmts.append(
            db.prepare(
                "INSERT INTO constituents "
                "(id, label, source_type, num_outcomes, probabilities, "
                "normalized_entropy, weight, volume_1mo, end_date, rank) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ).bind(
                c.id,
                c.label,
                c.source_type,
                c.num_outcomes,
                json.dumps([round(p, 4) for p in c.probabilities]),
                round(c.normalized_entropy, 4),
                round(c.weight, 6),
                round(c.volume_1mo, 2),
                c.end_date.isoformat() if c.end_date else JS_NULL,
                c.rank,
            )
        )

    # history (append)
    stmts.append(
        db.prepare(
            "INSERT OR IGNORE INTO history "
            "(timestamp, value, num_constituents, weighted_entropy, divisor) "
            "VALUES (?, ?, ?, ?, ?)"
        ).bind(
            state.timestamp.isoformat(),
            round(state.value, 2),
            state.num_constituents,
            round(state.weighted_entropy, 6),
            state.divisor,
        )
    )

    # state (full engine blob for resumption)
    state_data = {
        "value": state.value,
        "divisor": state.divisor,
        "weighted_entropy": state.weighted_entropy,
        "num_constituents": state.num_constituents,
        "timestamp": state.timestamp.isoformat(),
        "last_rebase": state.last_rebase.isoformat() if state.last_rebase else None,
        "constituents": [
            {
                "id": c.id,
                "label": c.label,
                "source_type": c.source_type,
                "num_outcomes": c.num_outcomes,
                "probabilities": c.probabilities,
                "volume_1mo": c.volume_1mo,
                "end_date": c.end_date.isoformat() if c.end_date else None,
                "weight": c.weight,
                "rank": c.rank,
                "normalized_entropy": c.normalized_entropy,
                "source_market_ids": c.source_market_ids,
            }
            for c in state.constituents
        ],
    }
    stmts.append(
        db.prepare("INSERT OR REPLACE INTO state (id, data) VALUES (1, ?)").bind(
            json.dumps(state_data)
        )
    )

    # adjustments (audit trail)
    ts = state.timestamp.isoformat()

    if is_first_run:
        adj_data = {
            "num_constituents": state.num_constituents,
            "index_value": round(state.value, 2),
            "divisor": state.divisor,
            "weighted_entropy": round(state.weighted_entropy, 6),
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        stmts.append(
            db.prepare(
                "INSERT INTO adjustments (timestamp, event, data) VALUES (?, ?, ?)"
            ).bind(ts, "initialization", json.dumps(adj_data))
        )

    elif state.rebased and prev_state is not None:
        # Bimonthly rebase — index reset to base value
        adj_data = {
            "divisor_before": prev_state.divisor,
            "divisor_after": state.divisor,
            "index_before": round(state.pre_adjustment_value, 2)
            if state.pre_adjustment_value is not None
            else round(prev_state.value, 2),
            "index_after": round(state.value, 2),
            "num_constituents": state.num_constituents,
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        stmts.append(
            db.prepare(
                "INSERT INTO adjustments (timestamp, event, data) VALUES (?, ?, ?)"
            ).bind(ts, "rebase", json.dumps(adj_data))
        )

    elif reconstitute and prev_state is not None:
        prev_ids = {c.id for c in prev_state.constituents}
        curr_ids = {c.id for c in state.constituents}
        added_ids = sorted(curr_ids - prev_ids)
        removed_ids = sorted(prev_ids - curr_ids)
        adj_data = {
            "divisor_before": prev_state.divisor,
            "divisor_after": state.divisor,
            "index_before": round(state.pre_adjustment_value, 2)
            if state.pre_adjustment_value is not None
            else round(prev_state.value, 2),
            "index_after": round(state.value, 2),
            "num_constituents": state.num_constituents,
            "added_ids": added_ids,
            "removed_ids": removed_ids,
            "added_count": len(added_ids),
            "removed_count": len(removed_ids),
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        stmts.append(
            db.prepare(
                "INSERT INTO adjustments (timestamp, event, data) VALUES (?, ?, ?)"
            ).bind(ts, "reconstitution", json.dumps(adj_data))
        )

    elif rebalance:
        weights = [c.weight for c in state.constituents]
        adj_data = {
            "divisor_before": prev_state.divisor,
            "divisor_after": state.divisor,
            "index_before": round(state.pre_adjustment_value, 2)
            if state.pre_adjustment_value is not None
            else round(prev_state.value, 2),
            "index_after": round(state.value, 2),
            "num_constituents": state.num_constituents,
            "weight_max": round(max(weights), 6),
            "weight_sum": round(sum(weights), 6),
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        stmts.append(
            db.prepare(
                "INSERT INTO adjustments (timestamp, event, data) VALUES (?, ?, ?)"
            ).bind(ts, "rebalance", json.dumps(adj_data))
        )

    elif state.removed_constituents:
        removed_ids = sorted(c.id for c in state.removed_constituents)
        adj_data = {
            "removed_ids": removed_ids,
            "removed_count": len(removed_ids),
            "divisor_before": prev_state.divisor,
            "divisor_after": state.divisor,
            "index_before": round(state.pre_adjustment_value, 2)
            if state.pre_adjustment_value is not None
            else round(prev_state.value, 2),
            "index_after": round(state.value, 2),
            "num_constituents": state.num_constituents,
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        stmts.append(
            db.prepare(
                "INSERT INTO adjustments (timestamp, event, data) VALUES (?, ?, ?)"
            ).bind(ts, "expiration_removal", json.dumps(adj_data))
        )

    await db.batch(stmts)


# ── CORS ─────────────────────────────────────────

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def json_response(data, status=200):
    """Return a JSON response with CORS headers."""
    return Response(
        json.dumps(data),
        status=status,
        headers={"Content-Type": "application/json", **CORS_HEADERS},
    )


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
        # Preserve recent detail while bounding D1 result serialization as the
        # append-only history table grows.
        try:
            query, values = build_history_query(since=since)
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)
        result = await self.env.DB.prepare(query).bind(*values).all()
        rows = result.results
        if hasattr(rows, "to_py"):
            rows = rows.to_py()
        return json_response(
            [
                {
                    "timestamp": r["timestamp"],
                    "value": r["value"],
                    "num_constituents": r["num_constituents"],
                    "weighted_entropy": r["weighted_entropy"],
                    "divisor": r["divisor"],
                }
                for r in rows
            ]
        )

    # ── Cron handler ─────────────────────────────

    async def scheduled(self, event, env, ctx):
        cron = event.cron

        if cron == "1 0 * * SUN":
            # Weekly reconstitution (+ rebalance) — Sunday 00:01 UTC
            await self._run_pipeline(reconstitute=True, rebalance=True)

        elif cron == "1 0 * * MON-SAT":
            # Daily rebalance — Mon-Sat only (Sunday handled by reconstitution cron)
            await self._run_pipeline(reconstitute=False, rebalance=True)

        elif cron == "*/5 * * * *":
            # Regular update — every 5 min (offset midnight crons avoid collision)
            await self._run_pipeline(reconstitute=False, rebalance=False)

        else:
            console.error(f"[gp300] unexpected cron schedule: {cron!r} — skipping")

    async def _run_pipeline(self, reconstitute=False, rebalance=False):
        try:
            db = self.env.DB
            now = datetime.now(timezone.utc)

            prev_state = await load_state_d1(db)
            is_first_run = prev_state is None
            if is_first_run and not reconstitute:
                reconstitute = True  # first run — fall back to init

            events = await fetch_events_async()
            console.log(f"[gp300] fetched {len(events)} events, reconstitute={reconstitute}, rebalance={rebalance}")

            state = run_full_pipeline(
                events=events,
                current_state=prev_state,
                now=now,
                reconstitute=reconstitute,
                rebalance=rebalance,
            )
            console.log(f"[gp300] pipeline done: value={state.value}, constituents={state.num_constituents}")

            await save_results_d1(
                db,
                state,
                prev_state=prev_state,
                reconstitute=reconstitute,
                rebalance=rebalance,
                is_first_run=is_first_run,
            )
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
