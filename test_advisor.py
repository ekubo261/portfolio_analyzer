#!/usr/bin/env python3
"""Offline checks for the advisor's non-model logic. No network, no API keys."""
from advisor import by_symbol, candidates, check, compact, levels

# Price facts
closes = [100 + i for i in range(250)]  # 100..349, steady uptrend
lv = levels(350, closes)
assert lv["high_52w"] == 349 and lv["low_52w"] == 100
assert lv["sma50"] == sum(closes[-50:]) / 50 and lv["sma200"] == sum(closes[-200:]) / 200
assert levels(10, [9, 10])["sma200"] is None, "short history must not fake an SMA"

# Level sanity: levels on the wrong side of the price are flagged
assert check({"buy_more_below": 90, "sell_above": 120}, 100, "buy_more_below", "sell_above") == []
assert len(check({"buy_more_below": 110, "sell_above": 95}, 100, "buy_more_below", "sell_above")) == 2
assert check({"buy_more_below": None, "sell_above": None}, 100, "buy_more_below", "sell_above") == []

# Article compaction strips Marketaux markup
art = lambda uid, *ents: {"uuid": uid, "title": "T", "published_at": "2026-09-28T10:00:00Z",
                          "source": "x.com", "entities": [
    {"symbol": s, "name": s, "sentiment_score": sc,
     "highlights": [{"highlight": "Big <em>gain</em> for X [+253 characters]"}]}
    for s, sc in ents]}
c = compact(art("u1", ("A.NS", 0.5)))
assert c["entities"][0]["highlights"] == ["Big gain for X"], c
assert c["date"] == "2026-09-28"

# The same article from two calls is counted once per symbol
plan = [{"articles": [art("u1", ("A.NS", 0.5), ("B.NS", -0.2))]},
        {"articles": [art("u1", ("A.NS", 0.5)), art("u2", ("A.NS", 0.1))]}]
g = by_symbol(plan)
assert len(g["A.NS"]) == 2 and len(g["B.NS"]) == 1

# Discovery candidates: held names excluded, strongest sentiment first
disc = [{"articles": [art("u3", ("H.NS", 0.9), ("W.NS", 0.2), ("S.NS", -0.8))]}]
assert candidates(disc, {"H.NS": {}}, 5) == ["S.NS", "W.NS"]
assert candidates(disc, {}, 1) == ["H.NS"]

print("all checks pass")
