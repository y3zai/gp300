from datetime import datetime, timezone

import click
import httpx

DEFAULT_BASE_URL = "https://gp300.y3z.ai"


def parse_timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise click.ClickException("Invalid ISO 8601 history timestamp") from error
    if timestamp.tzinfo is None:
        raise click.ClickException("History timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc)


def filter_history_since(data, since):
    if not since:
        return data
    cutoff = parse_timestamp(since)
    return [point for point in data if parse_timestamp(point["timestamp"]) >= cutoff]


class GP300Client:
    def __init__(self, base_url=DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=15.0,
            headers={"User-Agent": "gp300-cli/0.1.0"},
        )

    @property
    def is_archive(self):
        return self.base_url == DEFAULT_BASE_URL

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        try:
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            raise click.ClickException(f"Could not connect to {self.base_url}")
        except httpx.HTTPStatusError as e:
            raise click.ClickException(f"API error: {e.response.status_code}")
        except httpx.TimeoutException:
            raise click.ClickException("Request timed out")

    def get_current(self):
        if self.is_archive:
            raise click.ClickException(
                "Current index data is unavailable in the GP 300 archive. "
                "For the last observation, run 'gp300 history MAX --limit 1'."
            )
        return self._get("/api/current")

    def get_constituents(self):
        if self.is_archive:
            raise click.ClickException(
                "The constituent list is unavailable in the GP 300 archive. "
                "For the last recorded index value and count, run 'gp300 history MAX --limit 1'."
            )
        return self._get("/api/constituents")

    def get_history(self, since=None):
        if self.is_archive:
            return filter_history_since(self._get("/history.json"), since)
        params = {"since": since} if since else None
        return self._get("/api/history", params=params)
