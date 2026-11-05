import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from click.testing import CliRunner

from cli.api import GP300Client
from cli.main import cli


HISTORY = [
    {"timestamp": timestamp, "value": value}
    for value, timestamp in enumerate((
        "2023-12-31T23:59:59+00:00",
        "2024-01-01T00:00:00+00:00",
        "2024-02-29T11:59:59+00:00",
        "2024-02-29T12:00:00+00:00",
        "2024-03-30T11:59:59+00:00",
        "2024-03-30T12:00:00+00:00",
        "2024-03-31T12:00:00+00:00",
    ))
]


@pytest.fixture
def api_requests(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=HISTORY)

    httpx_client = httpx.Client
    monkeypatch.setattr(
        "cli.api.httpx.Client",
        lambda **kwargs: httpx_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.delenv("GP300_API_URL", raising=False)
    return requests


@pytest.mark.parametrize(
    ("history_args", "expected"),
    [
        ([], HISTORY[-2:]),
        (["1M"], HISTORY[3:]),
        (["YTD"], HISTORY[1:]),
        (["MAX", "--limit", "1"], HISTORY[-1:]),
    ],
)
def test_archive_history_uses_the_last_observation(api_requests, history_args, expected):
    result = CliRunner().invoke(cli, ["history", *history_args, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == expected
    assert [str(request.url) for request in api_requests] == ["https://gp300.y3z.ai/history.json"]


def test_archive_client_still_accepts_an_absolute_since(api_requests):
    assert GP300Client().get_history(since="2024-03-30T12:00:00Z") == HISTORY[-2:]
    assert str(api_requests[0].url) == "https://gp300.y3z.ai/history.json"


@pytest.mark.parametrize("command", [[], ["current"], ["constituents"]])
def test_default_archive_reports_unavailable_commands(api_requests, command):
    result = CliRunner().invoke(cli, command)

    assert result.exit_code == 1
    assert "archive" in result.output.lower()
    assert "gp300 history MAX --limit 1" in result.output
    assert not api_requests


def test_custom_history_still_requests_since_relative_to_now(api_requests):
    result = CliRunner().invoke(cli, ["--url", "https://custom.example", "history", "1D", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == HISTORY
    request, = api_requests
    assert request.url.host == "custom.example"
    assert request.url.path == "/api/history"
    since = datetime.fromisoformat(request.url.params["since"])
    assert abs(datetime.now(timezone.utc) - since - timedelta(days=1)) < timedelta(seconds=5)


def test_custom_client_still_reads_current_and_constituents(api_requests):
    client = GP300Client("https://custom.example/")
    assert client.get_current() == HISTORY
    assert client.get_constituents() == HISTORY
    assert [request.url.path for request in api_requests] == ["/api/current", "/api/constituents"]
