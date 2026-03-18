import click
import httpx

DEFAULT_BASE_URL = "https://gp300.y3z.ai"


class GP300Client:
    def __init__(self, base_url=DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=15.0,
            headers={"User-Agent": "gp300-cli/0.1.0"},
        )

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
        return self._get("/api/current")

    def get_constituents(self):
        return self._get("/api/constituents")

    def get_history(self, since=None):
        params = {"since": since} if since else None
        return self._get("/api/history", params=params)
