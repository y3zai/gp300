"""
G&P 300 Core Engine.

Handles:
- Constituent selection (eligibility filtering, ranking, buffer rule)
- Weight computation (volume1mo-weighted with 5% cap)
- Normalized entropy calculation (binary + multi-outcome)
- Index value computation with divisor
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from api import Market, Event


# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

@dataclass
class IndexConfig:
    """Tunable parameters for the G&P 300 index."""

    # Constituent selection
    target_count: int = 300
    buffer_top: int = 240      # ranks 1-240: unconditionally include
    buffer_bottom: int = 360   # ranks 361+: unconditionally remove

    # Eligibility
    min_volume_1mo: float = 1000.0       # minimum 30d volume in USD

    # Weighting
    weight_cap: float = 0.05  # 5% per-constituent cap

    # Index
    base_value: float = 1000.0


DEFAULT_CONFIG = IndexConfig()


# ──────────────────────────────────────────────
# Constituent: a unified representation
# ──────────────────────────────────────────────

@dataclass
class Constituent:
    """
    A single index constituent.

    For non-negRisk markets: 1 market = 1 constituent (binary, k=2)
    For negRisk events: 1 event = 1 constituent (multi-outcome, k=num_markets)
    """
    id: str                        # market_id or event_id
    label: str                     # question or event title
    source_type: str               # "market" or "event"

    # Outcomes
    num_outcomes: int              # k: 2 for binary, >2 for multi-outcome
    probabilities: list[float]     # [p1, p2, ...], should sum to ~1.0

    # Volume (for ranking & weighting)
    volume_1mo: float

    # Dates
    end_date: Optional[datetime]

    # Computed fields (filled by engine)
    normalized_entropy: float = 0.0
    weight: float = 0.0
    rank: int = 0

    # Reference back to source
    source_market_ids: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# Step 1: Build constituent universe
# ──────────────────────────────────────────────

def build_constituent_universe(
    events: list[Event],
    now: Optional[datetime] = None,
    config: IndexConfig = DEFAULT_CONFIG,
    eligibility_filter: bool = True,
) -> list[Constituent]:
    """
    Convert raw events/markets into a universe of eligible constituents.

    Processing rules:
    - negRisk events → aggregate into single multi-outcome constituent
    - non-negRisk markets → each market is an independent binary constituent

    Args:
        eligibility_filter: If True, apply volume minimum and activity checks.
            If False, skip those (used for regular updates so existing
            constituents aren't dropped by volume/activity changes).
            Expiry/active/closed checks always apply.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    universe = []

    for event in events:
        if event.neg_risk:
            # Multi-outcome: aggregate all active markets under this event
            constituent = _build_neg_risk_constituent(event, now, config, eligibility_filter)
            if constituent:
                universe.append(constituent)
        else:
            # Each market is independent
            for market in event.markets:
                constituent = _build_binary_constituent(market, now, config, eligibility_filter)
                if constituent:
                    universe.append(constituent)

    return universe


def _build_neg_risk_constituent(
    event: Event,
    now: datetime,
    config: IndexConfig,
    eligibility_filter: bool = True,
) -> Optional[Constituent]:
    """Build a multi-outcome constituent from a negRisk event."""

    # Skip expired events (always checked)
    if event.end_date and event.end_date < now:
        return None

    # Skip events that haven't launched yet (lookahead bias prevention)
    if event.start_date and event.start_date > now:
        return None

    # Only include active, non-closed markets
    active_markets = [m for m in event.markets if m.active and not m.closed]
    if len(active_markets) < 2:
        return None

    # Extract Yes prices as probabilities
    raw_probs = []
    market_ids = []
    for m in active_markets:
        if m.outcome_prices and len(m.outcome_prices) >= 1:
            raw_probs.append(m.outcome_prices[0])  # Yes price
            market_ids.append(m.id)

    if len(raw_probs) < 2:
        return None

    # Normalize probabilities to sum to 1.0
    total = sum(raw_probs)
    if total <= 0:
        return None
    probs = [p / total for p in raw_probs]

    # Volume: sum of market-level volume1mo
    total_vol_1mo = sum(m.volume_1mo for m in active_markets)

    # Volume minimum check (only when eligibility_filter is on)
    if eligibility_filter and total_vol_1mo < config.min_volume_1mo:
        return None

    # Activity check (only when eligibility_filter is on)
    if eligibility_filter and not any(m.volume_1wk > 0 for m in active_markets):
        return None

    return Constituent(
        id=event.id,
        label=event.title,
        source_type="event",
        num_outcomes=len(probs),
        probabilities=probs,
        volume_1mo=total_vol_1mo,
        end_date=event.end_date,
        source_market_ids=market_ids,
    )


def _build_binary_constituent(
    market: Market,
    now: datetime,
    config: IndexConfig,
    eligibility_filter: bool = True,
) -> Optional[Constituent]:
    """Build a binary constituent from a single market."""

    # Must be active and not closed (always checked)
    if not market.active or market.closed:
        return None

    # Skip expired markets (always checked)
    if market.end_date and market.end_date < now:
        return None

    # Skip markets that haven't launched yet (lookahead bias prevention)
    if market.start_date and market.start_date > now:
        return None

    # Must have valid prices
    if not market.outcome_prices or len(market.outcome_prices) < 2:
        return None

    p_yes = market.outcome_prices[0]
    p_no = market.outcome_prices[1]

    # Sanity check prices
    if p_yes < 0 or p_no < 0 or (p_yes + p_no) <= 0:
        return None

    # Normalize (they should be close to 1.0 already)
    total = p_yes + p_no
    probs = [p_yes / total, p_no / total]

    # Volume minimum check (only when eligibility_filter is on)
    if eligibility_filter and market.volume_1mo < config.min_volume_1mo:
        return None

    # Activity check (only when eligibility_filter is on)
    if eligibility_filter and market.volume_1wk <= 0:
        return None

    return Constituent(
        id=market.id,
        label=market.question,
        source_type="market",
        num_outcomes=2,
        probabilities=probs,
        volume_1mo=market.volume_1mo,
        end_date=market.end_date,
        source_market_ids=[market.id],
    )


# ──────────────────────────────────────────────
# Step 2: Rank and select constituents
# ──────────────────────────────────────────────

def rank_and_select(
    universe: list[Constituent],
    current_constituents: Optional[set[str]] = None,
    config: IndexConfig = DEFAULT_CONFIG,
) -> list[Constituent]:
    """
    Rank by volume1mo and apply the 240/360 buffer rule.

    Args:
        universe: Full eligible universe
        current_constituents: Set of IDs currently in the index (None = fresh start)
        config: Index configuration

    Returns:
        Selected constituents with rank assigned
    """
    # Sort by volume1mo descending
    ranked = sorted(universe, key=lambda c: c.volume_1mo, reverse=True)

    # Assign ranks
    for i, c in enumerate(ranked):
        c.rank = i + 1

    if current_constituents is None:
        # Fresh start: take top N
        selected = ranked[:config.target_count]
    else:
        # Apply buffer rule
        selected = []
        for c in ranked:
            if c.rank <= config.buffer_top:
                # Unconditionally include
                selected.append(c)
            elif c.rank <= config.buffer_bottom:
                # Keep if already in index, skip if not
                if c.id in current_constituents:
                    selected.append(c)
            # rank > buffer_bottom: unconditionally exclude

    return selected


# ──────────────────────────────────────────────
# Step 3: Compute weights
# ──────────────────────────────────────────────

def compute_weights(
    constituents: list[Constituent],
    config: IndexConfig = DEFAULT_CONFIG,
) -> list[Constituent]:
    """
    Compute volume1mo-weighted weights with iterative capping.

    Algorithm:
    1. Natural weights: w_i = volume1mo_i / total_volume1mo
    2. Cap any w_i > 5%
    3. Redistribute excess pro-rata to uncapped
    4. Repeat until converged
    """
    if not constituents:
        return constituents

    cap = config.weight_cap
    n = len(constituents)

    # Natural weights
    total_vol = sum(c.volume_1mo for c in constituents)
    if total_vol <= 0:
        # Equal weight fallback
        for c in constituents:
            c.weight = 1.0 / n
        return constituents

    weights = [c.volume_1mo / total_vol for c in constituents]

    # Iterative capping
    for _ in range(20):  # safety limit
        capped = [False] * n
        excess = 0.0

        for i in range(n):
            if weights[i] > cap:
                excess += weights[i] - cap
                weights[i] = cap
                capped[i] = True

        if excess < 1e-12:
            break

        # Redistribute excess pro-rata to uncapped
        uncapped_total = sum(weights[i] for i in range(n) if not capped[i])
        if uncapped_total <= 0:
            break

        for i in range(n):
            if not capped[i]:
                weights[i] += excess * (weights[i] / uncapped_total)

    # Ensure weights sum to exactly 1.0 after capping
    total = sum(weights)
    if abs(total - 1.0) > 1e-9 and total > 0:
        weights = [w / total for w in weights]

    # Assign
    for i, c in enumerate(constituents):
        c.weight = weights[i]

    return constituents


# ──────────────────────────────────────────────
# Step 4: Compute normalized entropy
# ──────────────────────────────────────────────

def normalized_entropy(probs: list[float]) -> float:
    """
    Compute normalized information entropy H ∈ [0, 1].

    H = (-Σ p_i log2(p_i)) / log2(k)

    where k = number of outcomes.

    Returns 0.0 for degenerate cases.
    """
    k = len(probs)
    if k < 2:
        return 0.0

    max_entropy = math.log2(k)
    if max_entropy <= 0:
        return 0.0

    raw_entropy = 0.0
    for p in probs:
        if p > 0:
            raw_entropy -= p * math.log2(p)

    return raw_entropy / max_entropy


def compute_entropy_all(constituents: list[Constituent]) -> list[Constituent]:
    """Compute normalized entropy for all constituents."""
    for c in constituents:
        c.normalized_entropy = normalized_entropy(c.probabilities)
    return constituents


# ──────────────────────────────────────────────
# Mid-cycle removal & weight redistribution
# ──────────────────────────────────────────────

def remove_dropped_constituents(
    old_constituents: list[Constituent],
    universe_map: dict[str, Constituent],
) -> tuple[list[Constituent], list[Constituent]]:
    """
    Match old constituents against the current universe.

    Returns (remaining, removed) where remaining have updated prices
    from the universe but preserve weight and rank from old state.
    """
    remaining, removed = [], []
    for old_c in old_constituents:
        if old_c.id in universe_map:
            new_c = universe_map[old_c.id]
            new_c.weight = old_c.weight
            new_c.rank = old_c.rank
            remaining.append(new_c)
        else:
            removed.append(old_c)
    return remaining, removed


def redistribute_weights(constituents: list[Constituent]) -> list[Constituent]:
    """Normalize weights pro-rata to sum to 1.0."""
    if not constituents:
        return constituents
    total = sum(c.weight for c in constituents)
    if total > 0:
        for c in constituents:
            c.weight /= total
    else:
        for c in constituents:
            c.weight = 1.0 / len(constituents)
    return constituents


# ──────────────────────────────────────────────
# Step 5: Index calculation
# ──────────────────────────────────────────────

@dataclass
class IndexState:
    """Current state of the G&P 300 index."""
    value: float
    divisor: float
    weighted_entropy: float  # numerator before division
    num_constituents: int
    timestamp: datetime
    constituents: list[Constituent]
    removed_constituents: list[Constituent] = field(default_factory=list)
    pre_adjustment_value: Optional[float] = None


def compute_weighted_entropy(constituents: list[Constituent]) -> float:
    """Compute Σ w_i * H_i (the numerator of the index formula)."""
    return sum(c.weight * c.normalized_entropy for c in constituents)


def initialize_index(
    constituents: list[Constituent],
    timestamp: Optional[datetime] = None,
    config: IndexConfig = DEFAULT_CONFIG,
) -> IndexState:
    """
    Initialize the index with a base value.

    D_initial = Σ(w_i * H_i) / base_value
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    we = compute_weighted_entropy(constituents)

    if we <= 0:
        # Edge case: all constituents have zero entropy
        divisor = 1.0 / config.base_value
    else:
        divisor = we / config.base_value

    return IndexState(
        value=config.base_value,
        divisor=divisor,
        weighted_entropy=we,
        num_constituents=len(constituents),
        timestamp=timestamp,
        constituents=constituents,
    )


def update_index_value(
    state: IndexState,
    constituents: list[Constituent],
    timestamp: Optional[datetime] = None,
) -> IndexState:
    """
    Recompute index value using current prices and fixed divisor/weights.

    Index = Σ(w_i * H_i) / D
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    we = compute_weighted_entropy(constituents)
    value = we / state.divisor if state.divisor > 0 else 0.0

    return IndexState(
        value=value,
        divisor=state.divisor,
        weighted_entropy=we,
        num_constituents=len(constituents),
        timestamp=timestamp,
        constituents=constituents,
    )


def adjust_divisor(
    state: IndexState,
    new_constituents: list[Constituent],
    pre_adj_value: Optional[float] = None,
) -> float:
    """
    Compute new divisor after a composition/weight change.

    D_new = Σ(w_new_i * H_i_current_prices) / Index_value_just_before

    This ensures zero discontinuity: the index value is the same
    immediately before and after the change.

    Args:
        pre_adj_value: If provided, use this as the anchor value instead of
            state.value. Needed when state.value is stale (from previous step).
    """
    anchor = pre_adj_value if pre_adj_value is not None else state.value
    if anchor <= 0:
        return state.divisor

    new_we = compute_weighted_entropy(new_constituents)
    return new_we / anchor


def _pre_adjustment_value(
    state: IndexState,
    universe_map: dict[str, Constituent],
) -> float:
    """Compute index value at current prices using old weights/divisor.

    Looks up each old constituent's current probabilities from universe_map,
    computes normalized_entropy at current prices, then applies old weights.
    Falls back to last-known entropy for constituents not in universe_map.
    """
    we = 0.0
    for c in state.constituents:
        if c.id in universe_map:
            h = normalized_entropy(universe_map[c.id].probabilities)
        else:
            h = c.normalized_entropy
        we += c.weight * h
    return we / state.divisor if state.divisor > 0 else state.value


# ──────────────────────────────────────────────
# Full pipeline
# ──────────────────────────────────────────────

def run_full_pipeline(
    events: list[Event],
    current_state: Optional[IndexState] = None,
    now: Optional[datetime] = None,
    config: IndexConfig = DEFAULT_CONFIG,
    reconstitute: bool = False,
    rebalance: bool = False,
) -> IndexState:
    """
    Run the complete index computation pipeline.

    Args:
        events: Raw events from API
        current_state: Previous index state (None = first run)
        now: Current timestamp
        config: Index configuration
        reconstitute: If True, re-select constituents (biweekly)
        rebalance: If True, recompute weights (weekly)

    Returns:
        New IndexState
    """
    if now is None:
        now = datetime.now(timezone.utc)

    is_first_run = current_state is None

    if is_first_run or reconstitute:
        # Step 1: Build universe (full eligibility filtering)
        universe = build_constituent_universe(events, now, config, eligibility_filter=True)

        # Step 2: Select constituents
        current_ids = None
        if current_state and not is_first_run:
            current_ids = {c.id for c in current_state.constituents}
        selected = rank_and_select(universe, current_ids, config)
    elif rebalance:
        # Relaxed universe (no volume/activity filter) so existing members
        # aren't dropped by transient volume changes
        universe = build_constituent_universe(events, now, config, eligibility_filter=False)
        universe_map = {c.id: c for c in universe}

        # Detect mid-cycle removals (expired/closed)
        selected, removed = remove_dropped_constituents(current_state.constituents, universe_map)

        if removed:
            # Redistribute weights before rebalance recomputes them anyway
            selected = redistribute_weights(selected)
    else:
        # Regular update: relaxed universe (skip volume/activity filter)
        universe = build_constituent_universe(events, now, config, eligibility_filter=False)
        universe_map = {c.id: c for c in universe}

        # Detect mid-cycle removals (expired/closed)
        selected, removed = remove_dropped_constituents(current_state.constituents, universe_map)

        if removed:
            # Redistribute weights pro-rata
            selected = redistribute_weights(selected)

            # All constituents removed — return proper removal state
            if not selected:
                pre_val = _pre_adjustment_value(current_state, universe_map)
                return IndexState(
                    value=0.0,
                    divisor=current_state.divisor,
                    weighted_entropy=0.0,
                    num_constituents=0,
                    timestamp=now,
                    constituents=[],
                    removed_constituents=removed,
                    pre_adjustment_value=pre_val,
                )

    # Build relaxed universe map for pre-adjustment value computation.
    # Needed so adjust_divisor anchors to the true current index value
    # rather than the stale state.value from the previous step.
    relaxed_map = None
    if not is_first_run:
        if rebalance or not reconstitute:
            # rebalance and regular already built relaxed universe → reuse
            relaxed_map = universe_map  # already {c.id: c}
        else:
            # reconstitute: build relaxed universe for pre-adj value
            relaxed = build_constituent_universe(events, now, config, eligibility_filter=False)
            relaxed_map = {c.id: c for c in relaxed}

    if not selected:
        # Degenerate case
        return IndexState(
            value=current_state.value if current_state else config.base_value,
            divisor=current_state.divisor if current_state else 1.0 / config.base_value,
            weighted_entropy=0.0,
            num_constituents=0,
            timestamp=now,
            constituents=[],
        )

    if is_first_run or rebalance or reconstitute:
        # Step 3: Compute weights
        selected = compute_weights(selected, config)

    # Step 4: Compute entropy
    selected = compute_entropy_all(selected)

    if is_first_run:
        # Step 5a: Initialize
        return initialize_index(selected, now, config)
    elif rebalance or reconstitute:
        # Step 5b: Adjust divisor, then compute
        pre_val = _pre_adjustment_value(current_state, relaxed_map)
        new_divisor = adjust_divisor(current_state, selected, pre_adj_value=pre_val)
        we = compute_weighted_entropy(selected)
        value = we / new_divisor if new_divisor > 0 else 0.0
        return IndexState(
            value=value,
            divisor=new_divisor,
            weighted_entropy=we,
            num_constituents=len(selected),
            timestamp=now,
            constituents=selected,
            pre_adjustment_value=pre_val,
        )
    else:
        # Step 5c: Regular update (prices only, possibly with removal)
        if removed:
            # Removals happened — adjust divisor to maintain continuity
            selected = compute_entropy_all(selected)  # already done above, but be safe
            pre_val = _pre_adjustment_value(current_state, relaxed_map)
            new_divisor = adjust_divisor(current_state, selected, pre_adj_value=pre_val)
            we = compute_weighted_entropy(selected)
            value = we / new_divisor if new_divisor > 0 else 0.0
            return IndexState(
                value=value,
                divisor=new_divisor,
                weighted_entropy=we,
                num_constituents=len(selected),
                timestamp=now,
                constituents=selected,
                removed_constituents=removed,
                pre_adjustment_value=pre_val,
            )
        else:
            return update_index_value(current_state, selected, now)
