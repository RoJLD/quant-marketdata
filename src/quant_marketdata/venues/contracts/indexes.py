"""Settlement indexes: the only place where a fixing method is declared, with its source and check date.

An index id is ``<provider>:<name>``: ``cfb:BRTI`` (CF Benchmarks, used by Kalshi), or ``<venue>:<raw>`` for
an index a venue computes itself (``deribit:btc_usd``, ``derive:BTC-USD``, ``paradex:BTC``). An id missing
from ``SETTLEMENT_INDEXES`` has method ``unknown``; comparisons log the index on both sides and never
equalize two indexes.

Decisions kept from the spec's open questions (§6, validated 2026-10-18 with their defaults):

- Q3 Paradex: the settlement price and method are not served nor documented -> ``paradex:<BASE>`` has method
  ``unknown`` (no entry below).
- Q4 Derive: the settlement method of its ``BTC-USD`` index is unknown -> ``unknown``.
- Q6 Deribit: delivery = TWAP of the index over the 30 minutes before 08:00 UTC, from the venue's
  documentation, still to be reconfirmed and dated -> ``venue_doc``.
- Kalshi: the method is read **per market** from ``rules_primary`` (``parse_fixing_method``): the rule
  changed between 2025 (60 s mean excluding the top and bottom 20 %) and 2026 (simple 60 s mean).
"""

from __future__ import annotations

import re
from typing import Dict, Tuple

IndexInfo = Dict[str, object]

SETTLEMENT_INDEXES: Dict[str, IndexInfo] = {
    "cfb:BRTI": {
        "name": "CF Benchmarks Bitcoin Real-Time Index",
        "method": "per_market",
        "method_source": "rules_text",
        "used_by": "kalshi KXBTCD, KXBTC",
        "verified": "2026-10-18",
        "note": "60 s before the fixing; simple mean (2026 markets) or mean excluding the top and bottom "
                "20 % (a 2025-01-01 market): read from each market's rules_primary",
    },
    "cfb:ERTI": {
        "name": "CF Benchmarks Ether Real-Time Index",
        "method": "per_market",
        "method_source": "rules_text",
        "used_by": "kalshi KXETHD, KXETH",
        "verified": "2026-10-18",
        "note": "as cfb:BRTI",
    },
    "deribit:btc_usd": {
        "name": "Deribit BTC/USD index",
        "method": "twap_30min",
        "method_source": "venue_doc",
        "used_by": "deribit BTC options",
        "verified": "2026-10-18",
        "note": "TWAP over the 30 minutes before 08:00 UTC per Deribit's documentation; to reconfirm (spec Q6)",
    },
    "deribit:eth_usd": {
        "name": "Deribit ETH/USD index",
        "method": "twap_30min",
        "method_source": "venue_doc",
        "used_by": "deribit ETH options",
        "verified": "2026-10-18",
        "note": "as deribit:btc_usd",
    },
    "derive:BTC-USD": {
        "name": "Derive BTC-USD index",
        "method": "unknown",
        "method_source": "unknown",
        "used_by": "derive BTC options",
        "verified": "2026-10-18",
        "note": "settlement prices served for the last 12 expiries only; method not documented (spec Q4)",
    },
    "derive:ETH-USD": {
        "name": "Derive ETH-USD index",
        "method": "unknown",
        "method_source": "unknown",
        "used_by": "derive ETH options",
        "verified": "2026-10-18",
        "note": "as derive:BTC-USD",
    },
}

_UNKNOWN: IndexInfo = {"method": "unknown", "method_source": "unknown"}

# Explicit patterns only, most specific first; anything else is ``unparsed`` and keeps the contract out of
# the scan families.
_FIXING_PATTERNS: Tuple[Tuple[re.Pattern, str, float], ...] = (
    (re.compile(r"average of the sixty seconds of .+?, excluding the top 20% and bottom 20% of values",
                re.IGNORECASE | re.DOTALL), "trimmed_mean_20pct", 60.0),
    (re.compile(r"simple average of the sixty seconds of ", re.IGNORECASE), "mean", 60.0),
)


def settlement_index_info(index_id: str) -> IndexInfo:
    """Declared method of an index; ``unknown`` for an id the table does not list."""
    return {**_UNKNOWN, **SETTLEMENT_INDEXES.get(index_id, {}), "settlement_index_id": index_id}


def venue_index_id(venue: str, raw: object) -> str:
    """Id of an index computed by the venue itself, from the name it serves (``deribit:btc_usd``)."""
    return f"{venue}:{raw}"


def parse_fixing_method(rules_text: object) -> Tuple[str, float]:
    """``(fixing_method, fixing_window_s)`` from a contract's rules text, by explicit patterns only:
    ``("trimmed_mean_20pct", 60)``, ``("mean", 60)``, or ``("unparsed", nan)``."""
    if not isinstance(rules_text, str):
        return "unparsed", float("nan")
    for pattern, method, window in _FIXING_PATTERNS:
        if pattern.search(rules_text):
            return method, window
    return "unparsed", float("nan")


__all__ = ["SETTLEMENT_INDEXES", "parse_fixing_method", "settlement_index_info", "venue_index_id"]
