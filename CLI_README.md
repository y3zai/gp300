# gp300

CLI for the [GP 300 Geopolitics Uncertainty Index](https://gp300.y3z.ai) — a real-time index tracking geopolitical uncertainty using prediction market data from Polymarket.

## Install

```bash
pip install gp300
```

## Usage

```bash
# Current index value
gp300

# Historical data (1D, 1W, 1M, 3M, YTD, 1Y, MAX)
gp300 history 1W
gp300 history MAX --limit 10

# List constituents
gp300 constituents
gp300 constituents --top 10 --sort entropy

# JSON output (for piping to jq, scripts, etc.)
gp300 current --json
gp300 constituents --json
```

## Commands

| Command | Description |
|---------|-------------|
| `gp300` | Show current index value (default) |
| `gp300 current` | Same as bare `gp300` |
| `gp300 history [RANGE]` | Historical data. Options: `--limit N` |
| `gp300 constituents` | List constituents. Options: `--top N`, `--sort [weight\|entropy\|volume\|name]` |

All commands support `--json` for raw JSON output and `-h` for help.
