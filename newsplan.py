#!/usr/bin/env python3
"""Spend a scarce Marketaux call budget well, and never spend it twice.

Marketaux free tier: 100 requests/day, hard cap of 3 articles per request
(https://www.marketaux.com/pricing). The article cap is per *request*, not per
symbol, so passing symbols=A,B,C still returns only 3 articles total - spread
across three names, some get nothing. One symbol per call is therefore the
right default; batching is a deliberate fallback for the tail of a long
portfolio, where thin signal beats no signal.
"""
import hashlib
import json
import os
import time
from math import ceil

NEWS_URL = "https://api.marketaux.com/v1/news/all"
ENTITY_URL = "https://api.marketaux.com/v1/entity/search"


def plan_calls(holdings, budget=30, per_call=3, batch_size=3, extra_pages=True,
               min_match=15):
    """-> list of {params, symbols, why}, len <= budget.

    holdings: [{"sym": str, "value": float}]  - value drives priority, because a
    call spent on your largest position is worth more than one spent on a tracker.
    """
    ranked = sorted(holdings, key=lambda h: -h["value"])
    n = len(ranked)

    # Give as many names as possible their own call; batch whatever is left over.
    solo = 0
    for i in range(n, -1, -1):
        if i + ceil((n - i) / batch_size) <= budget:
            solo = i
            break

    calls = []
    for h in ranked[:solo]:
        calls.append({"symbols": [h["sym"]], "page": 1,
                      "why": f"own call, position {h['value']:.0f}"})
    tail = ranked[solo:]
    for i in range(0, len(tail), batch_size):
        grp = [h["sym"] for h in tail[i:i + batch_size]]
        calls.append({"symbols": grp, "page": 1,
                      "why": f"batched tail ({len(grp)} names share {per_call} articles)"})

    # Budget left over on a short portfolio buys depth on the biggest positions:
    # page 2, 3... of the same query, which is new articles rather than repeats.
    if extra_pages:
        page = 2
        while len(calls) < budget and solo == n and n:
            for h in ranked:
                if len(calls) >= budget:
                    break
                calls.append({"symbols": [h["sym"]], "page": page,
                              "why": f"depth page {page} on position {h['value']:.0f}"})
            page += 1

    for c in calls:
        c["params"] = {"symbols": ",".join(c["symbols"]), "limit": per_call,
                       "page": c["page"], "filter_entities": "true",
                       "must_have_entities": "true", "language": "en",
                       "group_similar": "true",
                       # Below ~15 the stock is one name in a round-up list: its
                       # sentiment there is about the market, not the company.
                       "min_match_score": min_match}
    return calls[:budget]


def plan_discovery(budget=15, per_call=3, pos=0.3, neg=-0.3, countries="in"):
    """Half bullish, half bearish - paginated, because identical params return
    identical articles and would waste most of the budget."""
    calls, half = [], budget // 2
    for i in range(half):
        calls.append({"params": {"sentiment_gte": pos, "page": i + 1, "limit": per_call,
                                 "countries": countries, "filter_entities": "true",
                                 "must_have_entities": "true", "language": "en",
                                 "sort": "entity_sentiment_score", "sort_order": "desc"},
                      "side": "bullish", "why": f"positive sentiment, page {i + 1}"})
    for i in range(budget - half):
        calls.append({"params": {"sentiment_lte": neg, "page": i + 1, "limit": per_call,
                                 "countries": countries, "filter_entities": "true",
                                 "must_have_entities": "true", "language": "en",
                                 "sort": "entity_sentiment_score", "sort_order": "asc"},
                      "side": "bearish", "why": f"negative sentiment, page {i + 1}"})
    return calls


def unique(calls):
    """Distinct queries in a plan. A plan where this is < len(calls) is burning budget."""
    return {json.dumps(c["params"], sort_keys=True) for c in calls}


class Cache:
    """Disk cache keyed on the query. At 100 requests/day a repeated run must not
    re-spend the budget, so this is load-bearing, not an optimisation."""

    def __init__(self, path=".news_cache.json", ttl_hours=6):
        self.path, self.ttl = path, ttl_hours * 3600
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    @staticmethod
    def key(params):
        clean = {k: v for k, v in params.items() if k != "api_token"}
        return hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()[:16]

    def get(self, params):
        e = self.data.get(self.key(params))
        if e and time.time() - e["at"] < self.ttl:
            return e["articles"]
        return None

    def put(self, params, articles):
        self.data[self.key(params)] = {"at": time.time(), "articles": articles}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)  # atomic: a crash mid-write must not lose the cache


class RateLimitHit(Exception):
    """Marketaux answers 402 when the daily quota is gone. Must abort the run, not
    fall through as 'no news' - an empty result read as bearish is a fabricated signal."""
