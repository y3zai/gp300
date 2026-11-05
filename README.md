# GP 300 - Geopolitics Uncertainty Index

[![PyPI version](https://badge.fury.io/py/gp300.svg)](https://badge.fury.io/py/gp300)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A historical geopolitical uncertainty index built on prediction market data from [Polymarket](https://polymarket.com). The GP 300 tracked up to 300 actively traded geopolitics-related contracts to measure global geopolitical uncertainty.

The [GP 300 archive](https://gp300.y3z.ai) contains 70,958 observations from March 3 through November 4, 2026, Pacific time. The final observation was at 6:25:54 p.m. PST on November 4. [Download the history as JSON](https://gp300.y3z.ai/history.json).

## What is GP 300?

The GP 300 Index aggregated prediction market data to measure geopolitical uncertainty. It applied index methodology similar to the VIX and S&P 500 to prediction markets.

**Key Insights**:
- **Higher index value** → More geopolitical uncertainty
- **Lower index value** → Markets perceive outcomes as more predictable

**Coverage**: International relations, conflicts, sanctions, trade wars, territorial disputes, diplomatic negotiations, international organizations, and cross-border policy. Purely domestic political events are excluded.

## Features

- **Data-Driven**: Computed from Polymarket's public APIs
- **Volume-Weighted**: Constituents ranked by 30-day trading volume with 5% concentration cap
- **Entropy-Based**: Measures uncertainty using normalized information entropy
- **Multi-Outcome Support**: Handles both binary contracts and mutually-exclusive multi-outcome events
- **Python CLI**: Query index data from the command line
- **Web Archive**: Explore historical index values through the last recorded observation

## Quick Start

### Installation

```bash
pip install -e .
```

### Usage

```bash
# Historical data (1D, 1W, 1M, 3M, YTD, 1Y, MAX)
gp300 history 1W
gp300 history MAX --limit 10

# JSON output (for piping to jq, scripts, etc.)
gp300 history MAX --limit 1 --json
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `gp300 history [RANGE]` | Archived index observations. Options: `--limit N` |

The archive's relative ranges end at the final observation. See the [CLI reference](./CLI_README.md) for custom server options.

## Methodology

### Constituent Selection

The index tracks the **top 300 geopolitics-related contracts** from Polymarket, ranked by 30-day trading volume. A buffer rule (240/360 threshold) prevents excessive turnover during reconstitution.

### Index Calculation

1. **Volume Weighting**: Contracts are weighted by their 30-day trading volume
2. **Concentration Cap**: No single contract can exceed 5% of the index
3. **Entropy Measurement**: For each contract, calculate normalized information entropy:
   - Binary contracts: `H = -p*log₂(p) - (1-p)*log₂(1-p)`, normalized to [0,1]
   - Multi-outcome events: Full Shannon entropy across all outcomes, normalized
4. **Index Value**: Weighted average of constituent entropies × divisor

### Historical Update Cycles

- **5-minute updates**: Fetch latest prices and recompute index with frozen weights
- **Daily rebalance**: Recalculate weights based on current volume, apply 5% cap
- **Weekly reconstitution + rebalance**: Re-rank eligible contracts, add/remove constituents using buffer rule, and refresh weights

## Architecture

```
Polymarket APIs (Gamma, CLOB)
         ↓
    src/api.py (fetch events, markets, prices)
         ↓
    src/engine.py (select constituents, compute weights, entropy, index)
         ↓
    main.py (orchestrate update/rebalance/reconstitute cycles)
         ↓
    db.py (store in SQLite)
         ↓
    ├── public/index.html (web dashboard)
    └── cli/ (Python CLI tool)
```

## Repository Structure

```
gp300/
├── src/                    # Core index engine
│   ├── api.py             # Polymarket API client
│   └── engine.py          # Index computation logic
├── cli/                    # CLI tool (published to PyPI)
│   ├── main.py            # CLI commands (Click framework)
│   ├── api.py             # CLI-specific API wrapper
│   └── formatting.py      # Terminal output formatting (Rich library)
├── public/                 # Static web dashboard
│   ├── index.html         # Historical chart
│   └── history.json       # Frozen observations
├── main.py                # Orchestrator for index updates
├── backtest.py            # Historical simulation mode
├── db.py                  # SQLite database interface
├── test_engine.py         # Unit tests
├── schema.sql             # Database schema
├── wrangler.toml          # Cloudflare Workers config
└── gp300-plan.md          # Technical specification (40KB)
```

## Development

### Requirements

- Python 3.10+
- Dependencies: `httpx`, `click`, `rich`

### Local Setup

```bash
# Clone the repository
git clone https://github.com/y3zai/gp300.git
cd gp300

# Install the package and its CLI dependencies
pip install -e .

# Run the CLI locally
python -m cli history 1W

# Run tests
python -m pytest -q  # use python3 if your shell does not provide `python`
```

### Database

The local index engine uses SQLite. The schema includes tables for:
- Index snapshots and historical data
- Constituent details and weights
- Rebalance/reconstitution events
- Update state tracking

### Deployment

Cloudflare serves the files in `public/` as static assets. The archive uses
`/history.json`, with `/api/history` as a static alias for downloads. Both
URLs serve the complete archive. Clients apply date filters locally.

```bash
wrangler deploy
```

## Documentation

- **[gp300-plan.md](./gp300-plan.md)**: Comprehensive technical specification covering API design, index methodology, weighting, and update cycles
- **[CLI_README.md](./CLI_README.md)**: CLI-specific documentation

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests.

## License

This repository does not currently include a LICENSE file. Until a license is added, all rights are reserved unless stated otherwise by the author.

## Links

- **Web Dashboard**: [https://gp300.y3z.ai](https://gp300.y3z.ai)
- **PyPI Package**: [https://pypi.org/project/gp300/](https://pypi.org/project/gp300/)
- **GitHub Repository**: [https://github.com/y3zai/gp300](https://github.com/y3zai/gp300)
- **Polymarket**: [https://polymarket.com](https://polymarket.com)

## Author

Yu Zheng

---

*Inspired by traditional financial indices like the S&P 500 and VIX, applied to the emerging domain of prediction markets.*
