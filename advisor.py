#!/usr/bin/env python3
"""News -> hold/sell calls with price levels, plus buy/short ideas.

    plan (newsplan) -> fetch Marketaux news -> 1y prices from Yahoo -> one Claude call

A fixed pipeline, so no agent framework: the steps never change order and the
model makes one decision, not a sequence of them. Not financial advice.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse

import anthropic
import requests

from newsplan import NEWS_URL, Cache, RateLimitHit, plan_calls, plan_discovery
from portfolio import UA, load_holdings

CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{}?range=1y&interval=1d"


def load_env(path=".env"):
    """KEY=value lines into os.environ; real env vars win. Holds MARKETAUX_TOKEN
    and ANTHROPIC_API_KEY."""
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k and not k.startswith("#"):
                    os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- prices

def history(sym):
    """-> {price, closes}. One 1y chart call gives both the live price and the
    history the levels are anchored to."""
    r = requests.get(CHART.format(urllib.parse.quote(sym)), headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    return {"price": float(res["meta"]["regularMarketPrice"]), "closes": closes}


def levels(price, closes):
    """Facts the model must anchor to, so a 'sell above' is a real resistance
    level, not a number it made up."""
    sma = lambda n: round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None
    return {"price": round(price, 2), "high_52w": round(max(closes), 2),
            "low_52w": round(min(closes), 2), "sma50": sma(50), "sma200": sma(200),
            "chg_1m_pct": round((price / closes[-21] - 1) * 100, 1) if len(closes) > 21 else None}


# ---------------------------------------------------------------- news

def fetch(plan, tok, cache):
    """Fills c["articles"]. Marketaux allows 30 requests/minute on top of the daily
    quota, so fresh calls are spaced; cached ones cost nothing and don't wait."""
    for c in plan:
        arts = cache.get(c["params"])
        if arts is None:
            for attempt in range(2):
                r = requests.get(NEWS_URL, params={**c["params"], "api_token": tok}, timeout=30)
                if r.status_code == 429 and not attempt:
                    time.sleep(61)
                    continue
                break
            if r.status_code == 402:
                cache.save()  # keep what this run already paid for
                raise RateLimitHit(r.text)
            r.raise_for_status()
            arts = r.json()["data"]
            cache.put(c["params"], arts)
            time.sleep(2.1)  # 30/min
        c["articles"] = arts
    cache.save()
    return plan


def compact(a):
    """Just what the model needs: entity-level sentiment and the sentences behind it."""
    strip = lambda t: re.sub(r"</?em>|\[\+\d+ characters\]", "", t).strip()[:300]
    return {"title": a["title"], "date": a["published_at"][:10], "source": a["source"],
            "entities": [{"symbol": e["symbol"], "name": e["name"],
                          "sentiment": e["sentiment_score"],
                          "highlights": [strip(h["highlight"]) for h in e["highlights"][:2]]}
                         for e in a["entities"]]}


def by_symbol(plan):
    """Articles grouped under each symbol they mention, de-duplicated across calls."""
    out, seen = {}, set()
    for c in plan:
        for a in c["articles"]:
            for e in a["entities"]:
                if (a["uuid"], e["symbol"]) not in seen:
                    seen.add((a["uuid"], e["symbol"]))
                    out.setdefault(e["symbol"], []).append(compact(a))
    return out


def candidates(disc, held, n):
    """Discovery symbols worth pricing: strongest average sentiment, not already held."""
    scores = {}
    for c in disc:
        for a in c["articles"]:
            for e in a["entities"]:
                if e["symbol"] not in held and e["sentiment_score"] is not None:
                    scores.setdefault(e["symbol"], []).append(e["sentiment_score"])
    avg = {s: sum(v) / len(v) for s, v in scores.items()}
    return sorted(avg, key=lambda s: -abs(avg[s]))[:n]


# ---------------------------------------------------------------- model

LEVEL = {"type": ["number", "null"]}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "holdings", "ideas"],
    "properties": {
        "summary": {"type": "string"},
        "holdings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["symbol", "action", "sentiment", "confidence", "buy_more_below",
                         "sell_above", "stop_loss", "reasoning", "key_news"],
            "properties": {
                "symbol": {"type": "string"},
                "action": {"type": "string", "enum": ["add", "hold", "trim", "sell"]},
                "sentiment": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "buy_more_below": LEVEL, "sell_above": LEVEL, "stop_loss": LEVEL,
                "reasoning": {"type": "string"},
                "key_news": {"type": "array", "items": {"type": "string"}}}}},
        "ideas": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["symbol", "side", "confidence", "entry", "target", "stop_loss",
                         "reasoning", "key_news"],
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "short"]},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "entry": {"type": "number"}, "target": {"type": "number"},
                "stop_loss": {"type": "number"},
                "reasoning": {"type": "string"},
                "key_news": {"type": "array", "items": {"type": "string"}}}}},
    },
}

SYSTEM = """You are an equity analyst for an Indian retail investor. You get, per stock:
the position, price facts (live price, 52-week high/low, 50/200-day SMA, 1-month change)
and recent news with Marketaux entity sentiment (-1 to +1) and the sentences it came from.

For every holding give an action and price levels. Anchor every level to the price facts:
buy_more_below near support (SMA200, recent low), sell_above near resistance (52-week
high), stop_loss below support. Use null for a level you cannot justify. buy_more_below
must be under the live price and sell_above over it.

For ideas, pick only names where the news gives a concrete reason, and at most 5 per
side. Buy targets above entry with stop below; shorts the reverse.

Thin or stale news means low confidence and usually "hold" - never read an absence of
news as bearish. Quote the headlines you relied on in key_news. Be plain and specific."""


def advise(payload, model):
    client = anthropic.Anthropic()
    r = client.messages.create(
        model=model, max_tokens=16000, system=SYSTEM,
        output_config={"effort": "high",
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": json.dumps(payload, indent=1)}])
    if r.stop_reason == "refusal":
        sys.exit(f"Model declined: {r.stop_details}")
    if r.stop_reason == "max_tokens":
        sys.exit("Response hit max_tokens - fewer holdings or ideas per run")
    return json.loads(next(b.text for b in r.content if b.type == "text"))


def check(rec, price, lo_key, hi_key):
    """The model's levels, checked against the live price. A 'buy more below' above
    today's price is not a level, it's a mistake - flag it rather than show it."""
    lo, hi, bad = rec.get(lo_key), rec.get(hi_key), []
    if lo is not None and lo >= price:
        bad.append(f"{lo_key} {lo} >= price {price}")
    if hi is not None and hi <= price:
        bad.append(f"{hi_key} {hi} <= price {price}")
    return bad


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--holdings", default="holdings.yaml")
    p.add_argument("--calls", type=int, default=30, help="news calls on your portfolio")
    p.add_argument("--discovery", type=int, default=15, help="news calls for new ideas")
    p.add_argument("--per-call", type=int, default=3, help="articles per call")
    p.add_argument("--ideas", type=int, default=12, help="discovery names to price")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--plan", action="store_true", help="print the call plan and stop")
    a = p.parse_args()

    stocks = load_holdings(a.holdings).get("stocks") or []
    held, errors = {}, []
    for s in stocks:
        try:
            h = history(s["sym"])
        except Exception as e:
            errors.append(f"{s['sym']}: {e}")
            continue
        held[s["sym"]] = {"qty": s["qty"], "avg_cost": s.get("cost"),
                          "value": round(s["qty"] * h["price"]),
                          **levels(h["price"], h["closes"])}
    if not held:
        sys.exit("No stocks could be priced. " + "; ".join(errors))

    plan = plan_calls([{"sym": k, "value": v["value"]} for k, v in held.items()],
                      budget=a.calls, per_call=a.per_call)
    disc = plan_discovery(budget=a.discovery, per_call=a.per_call)
    if a.plan:
        for c in plan + disc:
            print(f"{c['why']:<45} {c['params'].get('symbols') or c['side']} p{c['params']['page']}")
        return 0

    load_env()
    # Check both keys before spending any quota: news fetched for a run that then
    # can't reach the model is budget burned.
    missing = [k for k in ("MARKETAUX_TOKEN", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing {', '.join(missing)} - add KEY=value lines to .env")
    tok, cache = os.environ["MARKETAUX_TOKEN"], Cache()
    print(f"Fetching news: {len(plan)} portfolio + {len(disc)} discovery calls "
          "(cached ones are free)...", file=sys.stderr)
    try:
        fetch(plan, tok, cache)
        fetch(disc, tok, cache)
    except RateLimitHit:
        sys.exit("Marketaux daily quota used up. Whatever was fetched is cached - rerun tomorrow.")

    news, disc_news = by_symbol(plan), by_symbol(disc)
    ideas = {}
    for sym in candidates(disc, held, a.ideas):
        try:
            h = history(sym)
            ideas[sym] = {**levels(h["price"], h["closes"]), "news": disc_news[sym]}
        except Exception as e:
            errors.append(f"{sym}: {e}")  # discovery symbols Yahoo doesn't know are skipped

    payload = {"holdings": {k: {**v, "news": news.get(k, [])} for k, v in held.items()},
               "idea_candidates": ideas}
    print(f"Asking {a.model}...", file=sys.stderr)
    out = advise(payload, a.model)

    prices = {**{k: v["price"] for k, v in held.items()},
              **{k: v["price"] for k, v in ideas.items()}}
    for r in out["holdings"]:
        r["warnings"] = check(r, prices.get(r["symbol"], 0), "buy_more_below", "sell_above")
    for r in out["ideas"]:
        lo, hi = ("stop_loss", "target") if r["side"] == "buy" else ("target", "stop_loss")
        r["warnings"] = check(r, prices.get(r["symbol"], r["entry"]), lo, hi)
    with open("advice.json", "w") as f:
        json.dump(out, f, indent=1)

    n = lambda x: "-" if x is None else f"{x:,.0f}"
    print(f"\n{out['summary']}\n")
    print(f"{'HOLDING':<14}{'ACTION':<7}{'SENT':<9}{'CONF':<7}{'PRICE':>8}"
          f"{'ADD <':>8}{'SELL >':>8}{'STOP':>8}")
    for r in out["holdings"]:
        print(f"{r['symbol']:<14}{r['action']:<7}{r['sentiment']:<9}{r['confidence']:<7}"
              f"{n(prices.get(r['symbol'])):>8}{n(r['buy_more_below']):>8}"
              f"{n(r['sell_above']):>8}{n(r['stop_loss']):>8}")
        print(f"  {r['reasoning']}")
        for w in r["warnings"]:
            print(f"  ! level ignored: {w}")
    print(f"\n{'IDEA':<14}{'SIDE':<7}{'CONF':<7}{'PRICE':>8}{'ENTRY':>8}{'TARGET':>8}{'STOP':>8}")
    for r in out["ideas"]:
        print(f"{r['symbol']:<14}{r['side']:<7}{r['confidence']:<7}{n(prices.get(r['symbol'])):>8}"
              f"{n(r['entry']):>8}{n(r['target']):>8}{n(r['stop_loss']):>8}")
        print(f"  {r['reasoning']}")
        for w in r["warnings"]:
            print(f"  ! level ignored: {w}")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    print("\nFull detail in advice.json. Generated from news sentiment - not financial advice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
