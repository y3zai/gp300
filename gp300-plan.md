# G&P 300 — GeoPolitics 300 Index

## Overview

G&P 300 is a prediction market index that tracks geopolitical uncertainty by aggregating data from the top 300 most actively traded geopolitics-related contracts on Polymarket. Inspired by traditional financial indices (S&P 500, VIX), it applies proven index methodology to the emerging domain of prediction markets.

The index outputs a single number representing the **overall level of geopolitical uncertainty** as priced by prediction market participants. A higher index value means more uncertainty; a lower value means the market considers outcomes more predictable.

**Scope**: The index covers geopolitics — international relations, conflicts, sanctions, trade wars, territorial disputes, diplomatic negotiations, international organizations, and cross-border policy. Purely domestic political events (e.g., state governor races, local referendums) are excluded.

---

## 1. Data Source

- **Platform**: Polymarket
- **Authentication**: None required for all data endpoints (Gamma API and CLOB public endpoints are fully public)

### 1.1 API Architecture

Polymarket exposes three separate APIs:

| API | Base URL | Purpose | Auth Required |
|---|---|---|---|
| **Gamma API** | `https://gamma-api.polymarket.com` | Market discovery, metadata, categories, tags, volume stats | No |
| **CLOB API** | `https://clob.polymarket.com` | Live prices, orderbooks, spreads, **historical price data** | No (read) |
| **Data API** | `https://data-api.polymarket.com` | User positions, trade history, open interest, holders | No |

### 1.2 Data Model (Verified)

Polymarket structures data in two levels:

- **Event**: A top-level question (e.g., "Venezuela leader end of 2026?"). Contains one or more Markets.
- **Market**: A specific tradable binary outcome within an Event. Each Market has a Yes/No pair with CLOB token IDs.

**Two types of multi-market events** (confirmed via API):

1. **negRisk events**: Mutually exclusive outcomes grouped under one event. Each is a binary market, but they collectively form a multi-outcome set. Example: "Venezuela leader end of 2026?" → 16 binary markets (Machado Yes/No, Cabello Yes/No, etc.). Identified by `negRisk: true` on the market objects.
2. **Time-series events**: Same question with different expiration dates. NOT mutually exclusive. Example: "US strikes Iran by...?" → 20 binary markets with dates ranging from Feb 5 to Jun 30. Identified by `negRisk: false` and `groupItemTitle` containing date labels.

**For our index**: We must treat these differently:
- negRisk events → Aggregate at Event level, extract probabilities from Yes prices, normalize to sum to 1.0, compute multi-outcome entropy
- Time-series events → Treat each market independently as a separate binary constituent

### 1.3 Key Endpoints We Will Use

**Gamma API — Market Discovery & Metadata**:

| Endpoint | What it gives us |
|---|---|
| `GET /events` | List events with filtering by `tag_id`, `end_date_min/max`, `active`, `closed` |
| `GET /events/{id}` | Single event with nested markets |
| `GET /markets` | List markets with filtering by `volume_num_min`, `liquidity_num_min`, `start_date_min/max`, `end_date_min/max` |
| `GET /tags` | Available tags/categories (for discovering geopolitics tag IDs) |

Key market fields from Gamma (verified):
```
id, question, slug, conditionId
outcomes          — '["Yes", "No"]' (stringified JSON array, must parse)
outcomePrices     — '["0.685", "0.315"]' (stringified JSON array, must parse)
clobTokenIds      — '["11407343...", "10249390..."]' (stringified, must parse)
volume            — total cumulative volume string, e.g. "20472026.644479"
volumeNum         — same as volume but as float
volume24hr        — 24-hour volume (float)
volume1wk         — 7-day volume (float)
volume1mo         — 30-day volume (float) ← CAN USE DIRECTLY, no accumulation needed!
volume1yr         — 1-year volume (float)
liquidity         — current liquidity string
liquidityNum      — same as liquidity but as float
endDate           — contract expiration (ISO 8601)
startDate         — market creation date
active, closed, archived — status flags
enableOrderBook   — whether CLOB trading is active
negRisk           — whether this is a negRisk (mutually exclusive multi-outcome) market
lastTradePrice    — last trade price (float)
oneDayPriceChange — 24h price change (float)
groupItemTitle    — label within an event group (e.g., "February 5", "March 31")
```

Key event fields from Gamma (verified):
```
id, title, slug
volume, volume24hr, volume1wk, volume1mo, volume1yr
liquidity, liquidityClob
endDate, startDate, creationDate
active, closed, archived
competitive
enableOrderBook
negRisk                — whether this event uses negRisk (mutually exclusive outcomes)
markets[]              — nested array of market objects
```

**CLOB API — Historical Prices (for backtest)**:

| Endpoint | What it gives us |
|---|---|
| `GET /prices-history` | Historical price timeseries for a token |

Parameters:
```
market     — CLOB token ID (required)
startTs    — Unix timestamp UTC (start of range)
endTs      — Unix timestamp UTC (end of range)
interval   — Duration ending at now: "1h", "6h", "1d", "1w", "1m", "max"
             (mutually exclusive with startTs/endTs)
fidelity   — Resolution in minutes. Verified minimum: 5 (not 1). 
             Tested: 5→2009pts/wk, 15→670, 30→336, 60→168
```

Response:
```json
{
  "history": [
    { "t": 1704067200, "p": 0.62 },
    { "t": 1704070800, "p": 0.64 }
  ]
}
```

**Note**: The last data point in any response is always the current live price, regardless of the endTs value. Factor this in when processing historical windows.

**Known limitation**: Resolved/closed markets may only return data at 12-hour+ granularity. Active markets support finer resolution (down to 5 minutes). For backtest, 30-minute fidelity (`fidelity=30`) on active markets works well.

### 1.4 Geopolitics Filtering Strategy (Verified)

Polymarket uses a **tag system** with hardcoded tag IDs. Confirmed tag IDs:

| Category | tag_id | Description |
|---|---|---|
| **Geopolitics** | **100265** | Primary tag for geopolitical events |
| Politics | 2 | Political markets (broader, includes domestic) |
| Finance | 120 | Financial markets |
| Crypto | 21 | Crypto markets |
| Sports | 100639 | Sports |
| Tech | 1401 | Technology |
| Culture | 596 | Entertainment, pop culture |

The Gamma API supports filtering by `tag_id` on the `/events` endpoint, with a `related_tags=true` parameter that automatically includes events from related tags:

```
GET /events?tag_id=100265&related_tags=true&closed=false&order=volume24hr&ascending=false&limit=100&offset=0
```

**Verified inventory** (as of 2026-02-27):
- **379 active geopolitics events** (tag_id=100265 alone)
- **1,644 total markets** within those events
- **974 active & not-closed markets** ← well above our 300 target
- Additional events via `related_tags=true` (18 related tags expand coverage)

Our filtering strategy:
1. **Primary**: `GET /events?tag_id=100265&related_tags=true&closed=false` — gets all geopolitics events plus events from 18 related tags in a single paginated stream
2. **Pagination**: Use `limit=100&offset=N` to page through all results (max 100 per page)
3. The API handles deduplication internally — no manual dedup needed

The `related_tags=true` parameter leverages the Gamma API's `/tags/{id}/related-tags/tags` endpoint, which returns 18 geopolitics-related tags:

| tag_id | Label | Slug |
|--------|-------|------|
| 78 | Iran | iran |
| 96 | Ukraine | ukraine |
| 103027 | Ukraine Peace Deal | ukraine-peace-deal |
| 102486 | Ukraine Map | ukraine-map |
| 61 | Gaza | gaza |
| 180 | Israel | israel |
| 154 | Middle East | middle-east |
| 738 | Yemen | yemen |
| 114 | Syria | syria |
| 102850 | Sudan | sudan |
| 303 | China | china |
| 166 | South Korea | south-korea |
| 101270 | Turkey | turkey |
| 246 | Venezuela | venezuela |
| 309 | Oil | oil |
| 101794 | Foreign Policy | foreign-policy |
| 102083 | India-Pakistan | india-pakistan |
| 102437 | Thailand-Cambodia | thailand-cambodia |

### 1.5 Volume Data (Verified)

The Gamma API provides the following volume fields per market (all confirmed via API):
- `volume` / `volumeNum` — Total cumulative volume (lifetime)
- `volume24hr` — Last 24 hours
- `volume1wk` — Last 7 days
- `volume1mo` — Last 30 days ← **Available directly! No accumulation needed.**
- `volume1yr` — Last year
- `liquidity` / `liquidityNum` — Current liquidity

**Decision**: Use `volume1mo` (30-day trailing volume) for ranking, which is exactly what we designed for. This is available directly from the Gamma API — no need to accumulate snapshots.

### 1.6 Rate Limits (Verified)

All limits are enforced via Cloudflare throttling (requests queued, not dropped):

| API | Endpoint | Limit |
|---|---|---|
| Gamma | `/events` | 500 requests / 10s |
| Gamma | `/markets` | 300 requests / 10s |
| Gamma | General | 4,000 requests / 10s |
| CLOB | `/prices-history` | 1,000 requests / 10s |
| CLOB | `/price` | 1,500 requests / 10s |
| CLOB | General | 9,000 requests / 10s |

For our use case (one batch fetch every 30 minutes), rate limits are a non-issue. A single update cycle:
- ~4 Gamma `/events` requests (paginated, 100 per page × 4 pages)
- 0 CLOB requests in normal mode (prices come from Gamma's `outcomePrices`)
- N CLOB `/prices-history` requests only during backtest (one per token per time window)

**Backtest concern**: Fetching historical prices for ~300+ tokens requires ~300+ CLOB requests per time step. At 1,000 req/10s, this completes in ~5 seconds. Across a 2-month backtest with 30-min steps (~2,880 steps), that's ~1.4M requests. Should batch by paginating time windows and add polite delays. Alternatively, fetch all history in one `interval=max` call per token upfront, then replay locally.

### 1.7 Multi-Outcome Contract Handling (Verified)

Polymarket has two distinct types of multi-market events:

**Type 1: negRisk events** (mutually exclusive outcomes)
- Identified by `negRisk: true` on market objects
- Example: "Venezuela leader end of 2026?" → 16 candidates, each a binary market
- In top 50 geopolitics events: 7 are negRisk, 43 are non-negRisk
- Pipeline: Aggregate at Event level, extract Yes prices, normalize, compute multi-outcome entropy

**Type 2: Time-series events** (independent markets with different dates)
- Identified by `negRisk: false` and multiple markets with `groupItemTitle` containing dates
- Example: "US strikes Iran by...?" → 20 markets with dates from Feb to Jun
- Pipeline: Treat each market as a **separate binary constituent** (they are independent bets)

**Processing logic**:
1. Fetch events via Gamma API
2. For each event, check if its markets have `negRisk: true`
3. If negRisk → Treat as one multi-outcome constituent:
   - Extract "Yes" price from each child market
   - Normalize: `p_i = yes_price_i / sum(all_yes_prices)` (they won't sum to exactly 1.0 due to spread)
   - Compute normalized entropy across k outcomes
   - Weight by event's aggregate `volume1mo`
4. If NOT negRisk → Each child market is a separate binary constituent:
   - Use its own `outcomePrices` directly
   - Compute binary entropy: H = -[p·log₂(p) + (1-p)·log₂(1-p)]
   - Weight by individual market's `volume1mo`

Example (negRisk):
```
Event: "Venezuela leader end of 2026?" (negRisk)
  Market 1: "Machado?" → Yes: 0.125
  Market 2: "Cabello?" → Yes: 0.01
  Market 3: "Figuera?" → Yes: 0.0105
  ... (16 total)

→ Normalize Yes prices to sum to 1.0
→ Compute normalized entropy: H = [-Σ(p_i × log₂(p_i))] / log₂(16)
→ Treat as ONE constituent in the index
```

Example (time-series, non-negRisk):
```
Event: "US strikes Iran by...?" (NOT negRisk)
  Market 1: "by Feb 28?" → Yes: 0.195, No: 0.805  → separate constituent, H = 0.73
  Market 2: "by Mar 31?" → Yes: 0.685, No: 0.315  → separate constituent, H = 0.90
  Market 3: "by Jun 30?" → Yes: 0.745, No: 0.255  → separate constituent, H = 0.83

→ Three separate binary constituents in the index
```

---

## 2. Constituent Selection

### 2.1 Universe

All active Events on Polymarket tagged with geopolitics-related tags. Fetched via Gamma API `GET /events?tag_id=100265&related_tags=true&active=true&closed=false`, which covers the primary Geopolitics tag plus 18 related tags (Iran, Ukraine, Gaza, Israel, Middle East, China, etc. — see Section 1.4 for the full table). The API handles deduplication internally. Constituent granularity depends on event type: **negRisk events** are aggregated at the Event level (one event = one multi-outcome constituent), while **non-negRisk events** have each child Market treated as a separate binary constituent (see Section 1.7 for details).

### 2.2 Eligibility Criteria

A contract must meet **all** of the following to be eligible:

| Criterion | Rule | Rationale |
|---|---|---|
| **Thematic relevance** | Must relate to geopolitics (v1: keyword/category filter; future: manual curation) | Ensures semantic coherence of the index |
| **Liquidity floor** | 30-day trading volume (`volume1mo`) > $1,000 | Filters out noise from thinly traded contracts |
| **Active trading** | Has had trading activity in the past 7 days (`volume_1wk > 0`) | Excludes stale/abandoned contracts |

### 2.3 Ranking & Selection with Buffer Rule

To avoid excessive churn from contracts hovering around the 300th rank, a **240/360 buffer rule** is applied during reconstitution:

```
Rank 1–240:    Unconditionally included (if not in index → add)
Rank 241–360:  Maintain status quo (if already in index → keep; if not → do not add)
Rank 361+:     Unconditionally removed (if in index → remove)
```

Ranking is by `volume1mo` (trailing 30-day trading volume) within the eligible universe, available directly from the Gamma API.

**Note**: The buffer rule means the index may temporarily have fewer than 300 constituents — for example, if constituents are removed from the 361+ zone but no new contracts from the 241–360 zone qualify for addition. This is acceptable and mirrors S&P 500 practice, where the index occasionally operates with fewer than 500 stocks between reconstitution events. Constituents will naturally replenish at the next reconstitution cycle.

### 2.4 Mid-Cycle Expiration Handling

Contracts that expire or resolve during the update cycle are handled as follows:

- **Expired or resolved**: Automatically removed when `end_date` has passed or market is closed/resolved
- **Divisor adjustment** is applied at the moment of removal to maintain index continuity
- **Weight redistribution**: The removed contract's weight is redistributed pro-rata to remaining constituents
- **No mid-cycle replacement**: Removed contracts are not replaced until the next scheduled reconstitution

---

## 3. Weighting

### 3.1 Method: Volume-Weighted with Cap

Each constituent's weight is proportional to its `volume1mo` (trailing 30-day trading volume), subject to a **5% per-contract cap**.

**Rationale**: Trading volume reflects the quality of price discovery — higher volume means more reliable probability signals. The cap prevents any single viral contract from dominating the index.

### 3.2 Capping Algorithm (Iterative)

```
1. Compute natural weights: w_i = volume1mo_i / total_volume1mo
2. Identify contracts where w_i > 5%
3. Cap those at 5%, compute total excess
4. Redistribute excess pro-rata to uncapped contracts
   (proportional to their current weights)
5. Repeat steps 2–4 until no contract exceeds 5%
```

Typically converges in 2–3 iterations.

### 3.3 Weight Freeze

Between rebalances, weights are **frozen**. Each 30-minute index update fetches the latest `outcomePrices` from the Gamma API but uses the fixed weights from the most recent rebalance.

---

## 4. Index Calculation

### 4.1 Core Metric: Weighted Normalized Information Entropy

To fairly compare contracts with different numbers of outcomes (binary Yes/No vs. multi-outcome), all entropy values are **normalized to [0, 1]**.

**For a contract with $k$ outcomes and probabilities $p_1, p_2, \ldots, p_k$:**

$$H_i = \frac{-\sum_{j=1}^{k} p_j \log_2 p_j}{\log_2 k}$$

- $H_i = 1$: all outcomes equally likely (maximum uncertainty)
- $H_i = 0$: one outcome has probability 1 (complete certainty)

**Binary contract** ($k = 2$) reduces to the familiar form:

$$H_i = \frac{-p \log_2 p - (1-p) \log_2 (1-p)}{\log_2 2} = -p \log_2 p - (1-p) \log_2 (1-p)$$

**Index value:**

$$\text{Index} = \frac{\sum_{i=1}^{N} w_i \cdot H_i}{D}$$

where $D$ is the divisor.

### 4.2 Normalization Examples

| Contract | Outcomes | Probabilities | Raw Entropy | Normalized Entropy |
|---|---|---|---|---|
| "Will US impose tariffs on China?" | 2 | [0.50, 0.50] | 1.00 bits | **1.00** |
| "Will US impose tariffs on China?" | 2 | [0.90, 0.10] | 0.47 bits | **0.47** |
| "Next UN Secretary General?" | 5 | [0.40, 0.30, 0.15, 0.10, 0.05] | 1.95 bits | **0.84** |
| "Next UN Secretary General?" | 5 | [0.20, 0.20, 0.20, 0.20, 0.20] | 2.32 bits | **1.00** |
| "Next UN Secretary General?" | 5 | [0.95, 0.02, 0.01, 0.01, 0.01] | 0.38 bits | **0.16** |

### 4.3 Base Value & Divisor

The index launches with a base value of **1000**. The divisor is calibrated at launch:

$$D_{\text{initial}} = \frac{\sum_{i=1}^{N} w_i \cdot H_i}{1000}$$

### 4.4 Divisor Adjustment

The divisor is recalculated whenever constituents or weights change:

$$D_{\text{new}} = \frac{V_{\text{new composition, current prices}}}{\text{Index Value just before change}}$$

This ensures **zero discontinuity** at every rebalance, reconstitution, or mid-cycle contract removal.

---

## 5. Update Cycles

Three separate cadences:

| Cycle | Frequency | What Happens | Divisor Adjusted? |
|---|---|---|---|
| **Data update** | Every 30 minutes | Pull latest probabilities, compute index value with current weights and constituents | No |
| **Weight rebalance** | Weekly | Recalculate `volume1mo` values, recompute weights with 5% cap | Yes |
| **Constituent reconstitution** | Biweekly | Re-rank eligible contracts, apply 240/360 buffer rule, add/remove constituents | Yes |
| **Expiration removal** | As needed | Remove expired/resolved contracts, redistribute weight | Yes |

---

## 6. Architecture & Deployment

### 6.1 Stack

```
Polymarket API
      │
      ▼
GitHub Actions (scheduled cron jobs)
      │
      ├── Python script: fetch data, compute index
      ├── Write results to JSON files
      ├── Commit & push to repo
      │
      ▼
GitHub Pages (static hosting)
      │
      ├── index.html (dashboard)
      ├── data/current.json (latest index value + metadata)
      ├── data/history.json (time series)
      └── data/constituents.json (current constituents + weights)
```

### 6.2 GitHub Actions Workflows

| Workflow | Schedule | Duration (est.) | Monthly Usage |
|---|---|---|---|
| Data update | Every 30 min | ~30s | ~720 min |
| Weight rebalance | Weekly | ~60s | ~4 min |
| Reconstitution | Biweekly | ~60s | ~2 min |
| **Total** | | | **~726 min** (well under 2,000 free limit) |

### 6.3 Cost

**$0**. Entirely within GitHub free tier.

### 6.4 Frontend

- Static HTML + JavaScript
- Chart library (D3.js or ECharts) for time series visualization
- Auto-refresh via polling (fetch updated JSON every 5 minutes)
- Cache-busting with `?t=timestamp` query parameter to bypass GitHub Pages CDN cache

### 6.5 Data Files

**`data/current.json`**:
```json
{
  "index_value": 1042.7,
  "timestamp": "2025-03-15T14:00:00Z",
  "num_constituents": 300,
  "weighted_entropy": 0.73,
  "divisor": 0.000699
}
```

**`data/history.jsonl`** (JSON Lines, append-only):
```
{"timestamp": "2025-03-15T13:00:00Z", "value": 1040.2, "num_constituents": 300, "weighted_entropy": 0.729841}
{"timestamp": "2025-03-15T14:00:00Z", "value": 1042.7, "num_constituents": 300, "weighted_entropy": 0.731205}
```

**`data/constituents.json`**:
```json
[
  {
    "id": "polymarket_contract_id",
    "label": "Will the US impose new tariffs on China by June 2025?",
    "source_type": "market",
    "num_outcomes": 2,
    "probabilities": [0.62, 0.38],
    "normalized_entropy": 0.959,
    "weight": 0.038,
    "volume_1mo": 2850000,
    "end_date": "2025-06-30T00:00:00+00:00",
    "rank": 5
  },
  {
    "id": "polymarket_event_id",
    "label": "Next UN Secretary General?",
    "source_type": "event",
    "num_outcomes": 5,
    "probabilities": [0.40, 0.30, 0.15, 0.10, 0.05],
    "normalized_entropy": 0.840,
    "weight": 0.022,
    "volume_1mo": 1200000,
    "end_date": "2025-09-15T00:00:00+00:00",
    "rank": 42
  }
]
```

---

## 7. Backtest Mode

Since the index updates on 30-minute, weekly, and biweekly cycles, waiting for real time to pass in order to debug is impractical. A **backtest mode** allows the entire pipeline to be validated using historical data in minutes.

### 7.1 Data Source

Polymarket API provides historical price/probability data. The backtest engine pulls this data for a specified date range and replays it as if time were advancing in fast-forward.

### 7.2 Usage

```bash
# Live mode: fetch real-time data, compute current index value
python main.py                    # Initialize (or reconstitute)
python main.py --update           # Regular 30-min price update
python main.py --rebalance        # Weekly: recompute weights
python main.py --reconstitute     # Biweekly: re-select constituents

# Backtest mode: simulate the full index lifecycle over a historical period
python backtest.py --start 2025-01-01 --end 2025-02-27 [--step 30] [--verbose]
```

### 7.3 What Backtest Mode Does

Starting from `--start`, the engine steps through time in 30-minute increments:

1. **Every 30-minute step**: Fetch historical probabilities at that timestamp, compute index value using current (frozen) weights and constituents
2. **Every weekly boundary**: Trigger weight rebalance — use `volume1mo` values, apply 5% cap, adjust divisor
3. **Every biweekly boundary**: Trigger constituent reconstitution — re-rank eligible contracts, apply 240/360 buffer rule, adjust divisor
4. **At every step**: Check for expired/resolved contracts, remove them, adjust divisor
5. **Output**: Full time series of index values, plus logs of every rebalance, reconstitution, and expiration event

### 7.4 Validation Checks

The backtest output should be inspected for:

| Check | What to look for | Indicates a bug if... |
|---|---|---|
| **Continuity** | Index value at rebalance/reconstitution boundaries | There is a jump or discontinuity at the exact moment of adjustment |
| **Divisor behavior** | Divisor values over time | Divisor changes at non-adjustment points, or doesn't change at adjustment points |
| **Constituent count** | Number of constituents over time | Count exceeds 360 (buffer_bottom), or drops to 0 |
| **Weight sum** | Sum of all weights at each step | Sum ≠ 1.0 (within floating-point tolerance) |
| **Weight cap** | Maximum weight at each step | Any weight exceeds 5% after a rebalance |
| **Entropy range** | Normalized entropy per contract | Any value outside [0, 1] |
| **Expiration removal** | Expired/resolved contracts | Still present in the index |

### 7.5 Output Files

Backtest produces the same JSON files as live mode, plus a diagnostic log:

**`data/backtest_log.json`**:
```json
[
  {
    "timestamp": "2025-01-07T00:00:00Z",
    "event": "rebalance",
    "divisor_before": 0.000700,
    "divisor_after": 0.000695,
    "index_before": 1000.0,
    "index_after": 1000.0,
    "num_constituents": 487,
    "weight_max": 0.05,
    "weight_sum": 1.0
  },
  {
    "timestamp": "2025-01-14T00:00:00Z",
    "event": "reconstitution",
    "added": ["contract_id_1", "contract_id_2"],
    "removed": ["contract_id_3"],
    "divisor_before": 0.000695,
    "divisor_after": 0.000688,
    "index_before": 1023.4,
    "index_after": 1023.4,
    "num_constituents": 488
  },
  {
    "timestamp": "2025-01-20T12:00:00Z",
    "event": "expiration_removal",
    "removed": ["contract_id_4"],
    "reason": "expired or resolved",
    "divisor_before": 0.000688,
    "divisor_after": 0.000685,
    "index_before": 1031.2,
    "index_after": 1031.2,
    "num_constituents": 487
  }
]
```

The key invariant to verify: **`index_before == index_after`** for every adjustment event.

---

## 8. Rollout Plan

### Phase 1: API Exploration ✅ COMPLETE
- [x] Study Polymarket API documentation
- [x] Identify available endpoints: Gamma (events, markets, tags), CLOB (prices, orderbook, price history), Data (positions, trades)
- [x] Geopolitics filtering: `tag_id=100265` on Gamma `/events` endpoint — confirmed working
- [x] Data quality: `volume1mo` (30-day trailing) available directly per market — no accumulation needed!
- [x] Multi-outcome representation: negRisk events = mutually exclusive multi-outcome; non-negRisk = independent binary
- [x] Historical price data: CLOB `/prices-history` — fidelity min 5min, supports startTs/endTs and `interval=max`
- [x] Rate limits verified: Gamma /events = 500 req/10s, CLOB price history = 1000 req/10s — extremely generous
- [x] Inventory verified: 379 geopolitics events, 974 active markets (well above 300 target)
- [x] No auth needed for all read endpoints — confirmed
- [x] Live API tests passed — all endpoints working as documented

### Phase 2: Core Engine (MVP) ✅ COMPLETE
- [x] Write Python script to fetch and filter contracts (`api.py` — 3-API client with parsing)
- [x] Implement constituent selection logic (`engine.py` — eligibility + ranking + 240/360 buffer)
- [x] Implement volume-weighted capping algorithm (iterative capping, verified sum=1.0, max=5%)
- [x] Implement normalized entropy calculation (binary k=2 + multi-outcome k=2..33, verified [0, 1] range)
- [x] Implement divisor-based index computation (init, update, adjust modes)
- [x] Output to JSON files (current.json, constituents.json, history.json, state.json)
- [x] Live test: ~300 constituents selected from 974 eligible, 6 capped at 5%, 16 multi-outcome events
- [x] Parameter adjustment: expiry filters removed entirely (no min/max expiry window)

### Phase 3: Backtest & Validation
- [ ] Implement backtest mode (--backtest --start --end flags)
- [ ] Run backtest over a 2–3 month historical window
- [ ] Verify continuity at all adjustment events (index_before == index_after)
- [ ] Verify weight sums, caps, entropy ranges, constituent counts
- [ ] Tune parameters (liquidity threshold, expiration window, cap percentage) based on backtest results
- [ ] Generate and review backtest_log.json for anomalies

### Phase 4: Automation
- [ ] Set up GitHub repository
- [ ] Configure GitHub Actions: 30-minute data update workflow
- [ ] Configure GitHub Actions: weekly rebalance workflow
- [ ] Configure GitHub Actions: biweekly reconstitution workflow
- [ ] Implement mid-cycle expiration removal logic
- [ ] Test end-to-end pipeline

### Phase 5: Frontend
- [ ] Build static dashboard (index value, time series chart, constituent table)
- [ ] Deploy to GitHub Pages
- [ ] Implement auto-refresh polling

### Phase 6: Refinement
- [ ] Add more visualizations (top movers, entropy distribution, constituent turnover stats)
- [ ] Consider additional indices (AI Progress Index, Global Economic Outlook Index, etc.)

---

## 9. Open Questions

1. ~~**Polymarket API coverage**~~ ✅ **Resolved**: Geopolitics tag_id = 100265. Directly filterable via `GET /events?tag_id=100265`.
2. ~~**Volume data granularity**~~ ✅ **Resolved**: `volume1mo` (30-day trailing) is available directly per market. No accumulation needed.
3. **Optimal thresholds**: Liquidity floor and exact expiration window bounds need tuning with backtest data.
4. ~~**Multi-outcome contract representation**~~ ✅ **Resolved**: Two types confirmed — negRisk events (mutually exclusive, aggregate at Event level) and time-series events (independent, each market is a separate constituent). Distinguished by `negRisk` field on market objects.
5. ~~**Overlapping contracts**~~ ✅ **Resolved**: Time-series events (e.g., "US strikes Iran by...?" with multiple dates) are NOT negRisk and should be treated as independent binary constituents. The 240/360 buffer rule and volume-based ranking will naturally manage which ones get included.
6. ~~**Initial constituent count**~~ ✅ **Resolved**: 974 active geopolitics markets available (379 events). Well above the 300 target. The index is viable at launch.
7. **Historical data for resolved markets**: CLOB `/prices-history` confirmed to return reduced granularity (12h+) for closed markets. **Backtest strategy**: Fetch full history (`interval=max`) for all tokens upfront once, then replay locally. For expired markets during the backtest window, accept 12h granularity or interpolate.
8. **Backtest data fetching strategy**: Fetching historical prices for ~300+ tokens individually is feasible (rate limits allow it) but time-intensive. Consider caching all historical data to disk in Phase 3 before running iterative backtest.
