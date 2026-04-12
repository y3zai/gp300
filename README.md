# GP 300 - Geopolitics Uncertainty Index

[![PyPI version](https://badge.fury.io/py/gp300.svg)](https://badge.fury.io/py/gp300)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A real-time geopolitical uncertainty index built on prediction market data from [Polymarket](https://polymarket.com). The GP 300 tracks the top 300 most actively traded geopolitics-related contracts to provide a single metric representing global geopolitical uncertainty.

**Live Dashboard**: [https://gp300.y3z.ai](https://gp300.y3z.ai)

## What is GP 300?

The GP 300 Index aggregates prediction market data to measure geopolitical uncertainty in real-time. Similar to how the VIX measures market volatility or the S&P 500 tracks stock performance, the GP 300 applies proven index methodology to the emerging domain of prediction markets.

**Key Insights**:
- **Higher index value** → More geopolitical uncertainty
- **Lower index value** → Markets perceive outcomes as more predictable

**Coverage**: International relations, conflicts, sanctions, trade wars, territorial disputes, diplomatic negotiations, international organizations, and cross-border policy. Purely domestic political events are excluded.

## Features

- **Data-Driven**: Real-time computation from Polymarket's public APIs
- **Volume-Weighted**: Constituents ranked by 30-day trading volume with 5% concentration cap
- **Entropy-Based**: Measures uncertainty using normalized information entropy
- **Multi-Outcome Support**: Handles both binary contracts and mutually-exclusive multi-outcome events
- **Fully Automated**: Updates via scheduled Cloudflare Workers every 5 minutes, with additional daily and weekly jobs
- **Python CLI**: Query index data from the command line
- **Web Dashboard**: Live visualization with historical charts and constituent breakdown

## Quick Start

### Installation

```bash
pip install gp300
```

### Usage

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

## CLI Commands

| Command | Description |
|---------|-------------|
| `gp300` | Show current index value (default) |
| `gp300 current` | Same as bare `gp300` |
| `gp300 history [RANGE]` | Historical data. Options: `--limit N` |
| `gp300 constituents` | List constituents. Options: `--top N`, `--sort [weight\|entropy\|volume\|name]` |

All commands support `--json` for raw JSON output and `-h` for help.

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

### Update Cycles

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
│   ├── engine.py          # Index computation logic
│   └── worker.py          # Cloudflare Workers entry point
├── cli/                    # CLI tool (published to PyPI)
│   ├── main.py            # CLI commands (Click framework)
│   ├── api.py             # CLI-specific API wrapper
│   └── formatting.py      # Terminal output formatting (Rich library)
├── public/                 # Static web dashboard
│   └── index.html         # Live dashboard with charts
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
python3 -m cli

# Run tests
python3 -m pytest -q
```

### Database

The project uses SQLite for local development and Cloudflare D1 for production. The schema includes tables for:
- Index snapshots and historical data
- Constituent details and weights
- Rebalance/reconstitution events
- Update state tracking

### Deployment

Production deployment uses Cloudflare Workers with:
- Python Workers for serverless execution
- D1 database for persistent storage
- KV storage for caching
- Cron triggers for scheduled updates

See `wrangler.toml` for configuration details.

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
