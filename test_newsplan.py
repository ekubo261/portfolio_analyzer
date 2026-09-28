#!/usr/bin/env python3
"""Offline checks for call-budget allocation. No API key, no network."""
import os
import tempfile

from newsplan import Cache, plan_calls, plan_discovery, unique

H = lambda n, base=1000: [{"sym": f"S{i}", "value": base * (n - i)} for i in range(n)]

# Short portfolio: every name gets its own call, leftover buys depth, budget respected
p = plan_calls(H(10), budget=30)
assert len(p) == 30, len(p)
assert len({c["symbols"][0] for c in p if c["page"] == 1}) == 10, "all 10 covered individually"
assert all(len(c["symbols"]) == 1 for c in p), "no batching needed at n=10"
assert max(c["page"] for c in p) > 1, "leftover budget should buy extra pages"

# Exactly at budget: one call each, no batching, no padding beyond budget
p = plan_calls(H(30), budget=30)
assert len(p) == 30 and all(len(c["symbols"]) == 1 and c["page"] == 1 for c in p)
assert len({c["symbols"][0] for c in p}) == 30

# Over budget: nothing is silently dropped - the tail gets batched instead
p = plan_calls(H(40), budget=30)
assert len(p) <= 30, len(p)
covered = {s for c in p for s in c["symbols"]}
assert len(covered) == 40, f"all 40 names must appear, got {len(covered)}"

# ...and the biggest positions are the ones that get a call to themselves
solo = {c["symbols"][0] for c in p if len(c["symbols"]) == 1}
batched = {s for c in p for s in c["symbols"] if len(c["symbols"]) > 1}
vals = {h["sym"]: h["value"] for h in H(40)}
assert min(vals[s] for s in solo) > max(vals[s] for s in batched), \
    "a batched position must never outrank a solo one"

# Pathological inputs
assert plan_calls([], budget=30) == []
assert len(plan_calls(H(1), budget=1)) == 1
assert len(plan_calls(H(100), budget=5)) <= 5
assert {s for c in plan_calls(H(9), budget=3) for s in c["symbols"]} == {f"S{i}" for i in range(9)}

# Discovery: every call must be a DISTINCT query. This is the bug in the original.
d = plan_discovery(budget=15)
assert len(d) == 15
assert len(unique(d)) == 15, f"only {len(unique(d))} unique queries - budget being burned"
assert sum(1 for c in d if c["side"] == "bullish") == 7
assert sum(1 for c in d if c["side"] == "bearish") == 8

# Portfolio plans must be distinct too
assert len(unique(plan_calls(H(10), budget=30))) == 30
assert len(unique(plan_calls(H(40), budget=30))) == len(plan_calls(H(40), budget=30))

# Cache round-trips, expires, and ignores api_token when keying
with tempfile.TemporaryDirectory() as d2:
    path = os.path.join(d2, "c.json")
    c = Cache(path, ttl_hours=6)
    q = {"symbols": "S0", "page": 1, "api_token": "SECRET"}
    assert c.get(q) is None
    c.put(q, [{"title": "x"}])
    assert c.get(q) == [{"title": "x"}]
    assert c.get({"symbols": "S0", "page": 1, "api_token": "DIFFERENT"}) == [{"title": "x"}], \
        "token must not affect the cache key"
    assert c.get({"symbols": "S0", "page": 2, "api_token": "SECRET"}) is None
    c.save()
    assert Cache(path).get(q) == [{"title": "x"}], "must survive a restart"
    assert Cache(path, ttl_hours=0).get(q) is None, "must expire"
    assert Cache(os.path.join(d2, "nope.json")).data == {}, "missing file is not an error"

print("all checks pass")
