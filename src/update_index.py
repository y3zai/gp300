"""Compute a scheduled GP300 update off-Worker and publish it through D1 REST."""

import argparse
import os
from datetime import datetime, time, timezone

import httpx

from .api import fetch_geopolitics_events
from .d1_state import STATE_QUERY, UpdateMode, build_d1_update, decode_state, select_update
from .engine import run_full_pipeline


class D1Client:
    def __init__(self, client, account_id, database_id, token):
        self.client = client
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self.headers = {"Authorization": f"Bearer {token}"}

    def query(self, body):
        response = self.client.post(self.url, json=body, headers=self.headers)
        response.raise_for_status()
        payload = response.json()
        results = payload.get("result") or []
        if not payload.get("success") or not results or not all(
            result.get("success") for result in results
        ):
            raise RuntimeError(f"D1 rejected the query: {payload.get('errors') or 'batch failed'}")
        return results

    def load(self):
        rows = self.query({"sql": STATE_QUERY})[0]["results"]
        return decode_state(rows[0]["data"] if rows else None)

    def publish(self, statements):
        # D1's REST API binds string parameters. SQLite column affinity
        # converts the numeric values when writing to typed columns.
        result = self.query({"batch": [
            {"sql": sql, "params": [str(value) for value in params]}
            for sql, params in statements
        ]})
        if len(result) != len(statements):
            raise RuntimeError("D1 returned an incomplete batch result")
        return bool(result[-1]["meta"]["changes"])


def fetch_events():
    return fetch_geopolitics_events(closed=False, active=True, limit=100, max_pages=20, delay=0)


def run_update(db, scheduled_at, *, now=None, dry_run=False):
    """Compute at observation time and publish once for the given UTC slot."""
    previous = db.load()
    now = now or datetime.now(timezone.utc)
    if scheduled_at > now:
        raise ValueError("scheduled slot is in the future")

    mode = select_update(previous, scheduled_at)
    if mode is None:
        if not dry_run:
            print(f"Skipping completed slot {scheduled_at.isoformat()}")
            return False
        # During staging a live Worker may already have published this slot.
        mode = UpdateMode(False, False, previous.maintenance_date)

    events = fetch_events()
    state = run_full_pipeline(
        events=events,
        current_state=previous.state if previous else None,
        now=now,
        reconstitute=mode.reconstitute,
        rebalance=mode.rebalance,
    )
    statements = build_d1_update(state, previous, scheduled_at, mode)
    print(
        f"Computed slot {scheduled_at.isoformat()} at {now.isoformat()} "
        f"from {len(events)} events with {state.num_constituents} constituents, "
        f"index {state.value:.2f}"
    )
    if dry_run:
        print("Dry run completed without writing to D1")
        return False

    applied = db.publish(statements)
    print("Published to D1" if applied else "Skipped because the state changed before publication")
    return applied


def parse_slot(value):
    try:
        slot = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an ISO 8601 timestamp") from error
    if slot.utcoffset() != timezone.utc.utcoffset(slot):
        raise argparse.ArgumentTypeError("scheduled slot must be in UTC")
    slot = slot.astimezone(timezone.utc)
    regular = slot.minute % 5 == 0 and slot.second == 0 and slot.microsecond == 0
    if not regular and slot.time() != time(0, 1):
        raise argparse.ArgumentTypeError("expected a five-minute boundary or 00:01 UTC")
    return slot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled-at", type=parse_slot, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    names = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN")
    if missing := [name for name in names if not os.environ.get(name)]:
        parser.error("missing environment variables: " + ", ".join(missing))
    with httpx.Client(timeout=60) as client:
        db = D1Client(client, *(os.environ[name] for name in names))
        run_update(db, args.scheduled_at, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
