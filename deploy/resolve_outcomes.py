#!/usr/bin/env python
"""Feed settled market outcomes into the reconciliation store (track record).

The server records every flagged opportunity, but it can't know how markets
*resolved* on its own — that truth lives at the venues. Run this periodically
(cron) once markets settle to close the loop, so track_record / GET /track-record
report real hit-rate, edge-slippage, Brier score, and the Merkle commitment.

Two ways to supply outcomes ({market_id: 0|1}, 1 = resolved YES):

  # from a JSON file
  python deploy/resolve_outcomes.py outcomes.json

  # from stdin
  echo '{"pm-btc-100k-2026": 1, "kx-fed-cut": 0}' | python deploy/resolve_outcomes.py -

Wire your own venue resolution fetch in `fetch_resolved()` for full autonomy
(Polymarket/Kalshi both expose resolution status on their public APIs).

Run inside the server container so it uses the same RECON_DB_URL:
  docker compose exec server python deploy/resolve_outcomes.py outcomes.json
"""

from __future__ import annotations

import json
import sys


def fetch_resolved() -> dict[str, int]:
    """Return {market_id: 0|1} for markets that have settled since last run.

    TODO: implement against the venue resolution APIs for hands-off operation.
    For now, outcomes are supplied via a file/stdin (see module docstring).
    """
    return {}


def load_outcomes(argv: list[str]) -> dict[str, int]:
    if len(argv) >= 2 and argv[1] not in ("-", ""):
        with open(argv[1]) as fh:
            return json.load(fh)
    if len(argv) >= 2 and argv[1] == "-":
        return json.load(sys.stdin)
    return fetch_resolved()


def main() -> int:
    outcomes = {str(k): int(v) for k, v in load_outcomes(sys.argv).items()}
    if not outcomes:
        print("no outcomes to resolve")
        return 0
    from predmarket_mcp import deps

    n = deps.resolve_outcomes(outcomes)
    print(f"resolved {n} opportunit{'y' if n == 1 else 'ies'} from {len(outcomes)} outcomes")
    print("track record:", json.dumps(deps.track_record_metrics()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
