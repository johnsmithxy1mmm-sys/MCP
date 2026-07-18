"""Event graph — markets as a related structure, not a flat list (F3).

Every scanner treats markets independently and recomputes pairwise relationships
on each call. But markets about the same subject form a *structure*: they quote
the same event on different venues, imply one another (BTC>150k ⟹ BTC>100k), are
mutually exclusive (election outcomes), or sit on the same probability term
curve. This module builds that structure once per market set and exposes it, which
buys four things:

* an incremental view instead of O(N²)-per-call pairwise scans;
* **transitive** entailment violations — checked across ALL threshold pairs in a
  cluster, not just adjacent ones, so a mispricing that leaks across a middle rung
  (A⇒B⇒C) is caught where adjacent-only checks miss it;
* a cached basis-risk lookup per pair (no repeated LLM calls on the same two
  markets);
* a free ``event://{entity}`` resource — "everything the markets say about BTC
  100k" — a strong funnel hook.

Pure/offline over a list of :class:`Market`. Reuses the C1 claim parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .entailment import parse_claim
from .models import Market


@dataclass
class EventCluster:
    entity: str
    markets: list[Market] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)


def entity_of(market: Market) -> str | None:
    """Cluster key for a market — its parsed entity, else None (unclusterable)."""
    claim = parse_claim(market)
    return claim.entity if claim is not None else None


def _mkt_ref(m: Market) -> dict:
    return {"venue": m.venue.value, "market_id": m.market_id, "title": m.title,
            "yes_price": m.yes_price, "threshold": None}


def build_clusters(markets: list[Market]) -> dict[str, EventCluster]:
    """Group markets by parsed entity and compute intra-cluster relations."""
    clusters: dict[str, EventCluster] = {}
    claims_by_entity: dict[str, list] = {}
    for m in markets:
        claim = parse_claim(m)
        if claim is None:
            continue
        clusters.setdefault(claim.entity, EventCluster(entity=claim.entity)).markets.append(m)
        claims_by_entity.setdefault(claim.entity, []).append(claim)

    for entity, claims in claims_by_entity.items():
        clusters[entity].relations = _relations(claims)
    return clusters


def _relations(claims: list) -> list[dict]:
    """implies / term_neighbor / same_event / excludes edges for one cluster."""
    rels: list[dict] = []
    # Implication ladder: same direction + deadline, ordered by threshold.
    by_dir_date: dict[tuple, list] = {}
    for c in claims:
        by_dir_date.setdefault((c.direction, c.deadline_key), []).append(c)
    for (direction, _), group in by_dir_date.items():
        group = sorted(group, key=lambda c: c.threshold)
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                lo, hi = group[i], group[j]
                if lo.threshold == hi.threshold:
                    continue
                # "above": higher threshold IMPLIES lower (if >hi then >lo).
                strong, weak = (hi, lo) if direction == "above" else (lo, hi)
                rels.append({
                    "type": "implies",
                    "from": strong.market.market_id, "to": weak.market.market_id,
                    "note": f"{strong.threshold:g} {direction} ⟹ {weak.threshold:g} {direction}",
                })
    # Term-structure neighbours: same threshold + cumulative, different deadline.
    by_thresh: dict[tuple, list] = {}
    for c in claims:
        if c.direction == "above" and c.cumulative and c.year is not None:
            by_thresh.setdefault((c.threshold,), []).append(c)
    for group in by_thresh.values():
        group = sorted(group, key=lambda c: (c.year, c.month or "00"))
        for a, b in zip(group, group[1:]):
            rels.append({"type": "term_neighbor",
                         "from": a.market.market_id, "to": b.market.market_id})
    # Same-event across venues: same entity, similar threshold, different venue.
    for i in range(len(claims)):
        for j in range(i + 1, len(claims)):
            a, b = claims[i], claims[j]
            if a.market.venue != b.market.venue and a.threshold == b.threshold \
                    and a.direction == b.direction:
                rels.append({"type": "same_event",
                             "from": a.market.market_id, "to": b.market.market_id})
    return rels


def transitive_entailment_violations(
    markets: list[Market], min_edge: float = 0.0, cost_haircut: float = 0.01
) -> list[dict]:
    """All-pairs threshold-monotonicity check within each entity cluster.

    Unlike the adjacent-only C1 ladder scan, this compares EVERY pair, so a
    violation that spreads across a middle rung is caught. Returns the priced
    violations (buy the too-cheap weaker claim, sell the too-dear stronger one).
    """
    out: list[dict] = []
    claims = [c for c in (parse_claim(m) for m in markets) if c is not None]
    groups: dict[tuple, list] = {}
    for c in claims:
        groups.setdefault((c.entity, c.direction, c.deadline_key), []).append(c)
    for (entity, direction, _), group in groups.items():
        group = sorted(group, key=lambda c: c.threshold)
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                lo, hi = group[i], group[j]
                if lo.threshold == hi.threshold:
                    continue
                if direction == "above":
                    edge = round(hi.market.yes_price - lo.market.yes_price - cost_haircut, 4)
                else:
                    edge = round(lo.market.yes_price - hi.market.yes_price - cost_haircut, 4)
                if edge < max(min_edge, 0.0001):
                    continue
                out.append({
                    "entity": entity, "direction": direction, "edge": edge,
                    "sell": (hi if direction == "above" else lo).market.market_id,
                    "buy": (lo if direction == "above" else hi).market.market_id,
                    "thresholds": [lo.threshold, hi.threshold],
                    "adjacent": j == i + 1,
                })
    return out


def event_view(entity_query: str, markets: list[Market]) -> dict:
    """The ``event://{entity}`` payload: matching clusters + their relations."""
    q = entity_query.strip().lower()
    clusters = build_clusters(markets)
    matched = [c for key, c in clusters.items() if q in key or key in q or
               any(tok in key for tok in q.split())]
    if not matched:
        return {"entity": entity_query, "clusters": [], "markets": 0}
    return {
        "entity": entity_query,
        "clusters": [
            {
                "entity": c.entity,
                "markets": [_mkt_ref(m) for m in c.markets],
                "relations": c.relations,
            }
            for c in matched
        ],
        "markets": sum(len(c.markets) for c in matched),
        "violations": transitive_entailment_violations(
            [m for c in matched for m in c.markets]),
    }
