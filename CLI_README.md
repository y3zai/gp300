# gp300

CLI for the archived GP 300 Geopolitics Uncertainty Index, which tracked geopolitical uncertainty using Polymarket prediction markets. [Public archive](https://gp300.y3z.ai)

## Install from this checkout

```bash
python -m pip install -e .
```

## Browse the archive

```bash
# Historical data (1D, 1W, 1M, 3M, YTD, 1Y, MAX)
gp300 history 1W
gp300 history MAX --limit 10

# Last recorded observation
gp300 history MAX --limit 1 --json
```

The public site serves frozen observations from `/history.json`. The CLI filters history locally using the last observation's UTC timestamp as the end of each range. `1D` is the default, `MAX` returns the full archive, and `--limit N` keeps the most recent N rows after filtering. The archive provides historical observations only.

Earlier PyPI releases can read the archive through the legacy `/api/history` route. Use this checkout for archive-aware range filtering.

## Use an alternate API server

Set `--url` or `GP300_API_URL` to the base URL of a server that implements the original GP 300 API. History ranges on alternate servers are relative to the current UTC time.

```bash
gp300 --url https://gp300.example.com current --json
gp300 --url https://gp300.example.com constituents --top 10 --sort entropy
GP300_API_URL=https://gp300.example.com gp300 history 1W
```

## Commands

| Command | Description |
|---------|-------------|
| `gp300 history [RANGE]` | Show observations for the selected range, with optional `--limit N` |
| `gp300 current` or `gp300` | Show the current index value on an alternate API server |
| `gp300 constituents` | List constituents on an alternate API server, with optional `--top N` and `--sort [weight\|entropy\|volume\|name]` |

All commands support `--json` for raw JSON output and `-h` for help.
