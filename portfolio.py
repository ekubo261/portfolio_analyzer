#!/usr/bin/env python3
"""Single-page portfolio view: stocks, funds, PPF/EPF/NPS, FDs, gold, silver.

No logins anywhere. Holdings come from a local YAML file you edit; only prices
are fetched, from public endpoints that need no key.
"""
import argparse
import calendar
import sys
import urllib.parse
from datetime import date, datetime

import requests
import yaml

# Yahoo answers 429 to curl's default User-Agent. A browser UA gets 200, so this
# header is load-bearing, not decoration.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
YAHOO = "https://query2.finance.yahoo.com/v8/finance/chart/{}?range=1d&interval=1d"
MFAPI = "https://api.mfapi.in/mf/{}"
MFSEARCH = "https://api.mfapi.in/mf/search?q={}"
OZ_G = 31.1035  # grams per troy ounce

# AMFI's own NAVAll.txt is blocked by corp TLS interception; mfapi mirrors it.
# NPS Trust is blocked too, which is why NPS is a manual balance below.

ERRORS = []


# ---------------------------------------------------------------- holdings

def load_holdings(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        sys.exit(f"No {path}. Start with:  cp holdings.example.yaml {path}")
    # ponytail: YAML only. CAS import slots in here as a second loader
    # returning the same dict shape.


def as_date(v):
    """YAML gives a date for unquoted 2026-03-31, a str if quoted."""
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v), "%Y-%m-%d").date()


# ---------------------------------------------------------------- prices

def quote(symbol):
    """-> (price, prev_close, name). Yahoo meta has chartPreviousClose, not previousClose."""
    r = requests.get(YAHOO.format(urllib.parse.quote(symbol)), headers=UA, timeout=20)
    r.raise_for_status()
    m = r.json()["chart"]["result"][0]["meta"]
    return (float(m["regularMarketPrice"]),
            float(m.get("chartPreviousClose") or m["regularMarketPrice"]),
            m.get("shortName") or symbol)


def mf_nav(code):
    """-> (nav, prev_nav, scheme_name, nav_date). Full history in one call gives
    the previous NAV for day change, which /latest cannot."""
    r = requests.get(MFAPI.format(code), headers=UA, timeout=20)
    r.raise_for_status()
    d = r.json()
    pts = d["data"]
    if not pts:
        raise ValueError("no such scheme code - find it with --find")
    prev = float(pts[1]["nav"]) if len(pts) > 1 else float(pts[0]["nav"])
    return float(pts[0]["nav"]), prev, d["meta"]["scheme_name"], pts[0]["date"]


def metal_inr_per_g(usd_per_oz, usdinr, premium_pct):
    """International spot sits below Indian retail because of import duty and GST,
    so premium_pct is the calibration knob you set once against a local rate."""
    return usd_per_oz * usdinr / OZ_G * (1 + premium_pct / 100)


# ---------------------------------------------------------------- accrual math

def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def fd_value(principal, rate, start, months, today=None):
    """Quarterly compounding (Indian bank convention), frozen at maturity."""
    today = today or date.today()
    maturity = add_months(start, months)
    term = (maturity - start).days
    done = max(0, (min(today, maturity) - start).days) / term
    # Fraction of the term, not days/365.25: that drifts half a day a year and
    # lands on 7.9945 quarters at a 2-year maturity instead of exactly 8.
    return principal * (1 + rate / 400) ** (months / 3 * done)


def accrue(balance, asof, rate, today=None):
    """Annual compounding from a stated balance. PPF/EPF."""
    if not rate:
        return balance
    today = today or date.today()
    years = max(0.0, (today - asof).days / 365.25)
    return balance * (1 + rate / 100) ** years
    # ponytail: ignores in-year contributions. Refresh `asof` once a year and the
    # drift goes away; modelling a contribution schedule is real work for an estimate.


# ---------------------------------------------------------------- formatting

def fmt_inr(n):
    """Indian digit grouping: 1234567 -> '12,34,567'."""
    n = int(round(n))
    s = str(abs(n))
    if len(s) <= 3:
        body = s
    else:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        body = ",".join(groups + [tail])
    return ("-" if n < 0 else "") + body


def pct(x):
    return f"{x:+.2f}%"


# ---------------------------------------------------------------- build

def safe(label, fn):
    """One bad symbol or stale scheme code must not kill the whole page."""
    try:
        return fn()
    except Exception as e:
        ERRORS.append(f"{label}: {type(e).__name__}: {e}")
        return None


def build(h):
    """-> (sections, nav_dates). Each section carries its own subtotal."""
    sections, nav_dates = [], []

    rows = []
    for s in h.get("stocks") or []:
        q = safe(s["sym"], lambda s=s: quote(s["sym"]))
        if not q:
            continue
        price, prev, name = q
        qty, cost = s["qty"], s.get("cost")
        rows.append({"name": name, "sub": s["sym"], "qty": qty, "price": price,
                     "value": qty * price, "prev": qty * prev,
                     "gain": (price / cost - 1) * 100 if cost else None})
    if rows:
        sections.append({"title": "Stocks", "bucket": "Stocks", "unit": "qty", "rows": rows})

    rows = []
    for f in h.get("funds") or []:
        q = safe(f"scheme {f['code']}", lambda f=f: mf_nav(f["code"]))
        if not q:
            continue
        nav, prev, name, nav_date = q
        nav_dates.append((name, nav_date))
        units, cost = f["units"], f.get("cost")
        rows.append({"name": name, "sub": str(f["code"]), "qty": units, "price": nav,
                     "value": units * nav, "prev": units * prev,
                     "gain": (nav / cost - 1) * 100 if cost else None})
    if rows:
        sections.append({"title": "Mutual Funds", "bucket": "Mutual Funds",
                         "unit": "units", "rows": rows})

    # Retirement + FDs: no fetchable price anywhere, and no honest cost basis
    # (their "cost" is a contribution stream), so value only, no gain column.
    rows = []
    for key, label in (("epf", "EPF"), ("ppf", "PPF"), ("nps", "NPS")):
        a = h.get(key)
        if not a:
            continue
        bal = a["balance"]
        if a.get("asof") and a.get("rate"):
            val = accrue(bal, as_date(a["asof"]), a["rate"])
            note = f"accrued from {as_date(a['asof'])} at {a['rate']}%"
        else:
            val = bal
            note = "as entered" + (" (NAV source blocked)" if key == "nps" else "")
        rows.append({"name": label, "sub": note, "value": val, "prev": val})
    if rows:
        sections.append({"title": "Retirement", "bucket": "Retirement", "rows": rows})

    rows = []
    for d in h.get("fd") or []:
        start, months = as_date(d["start"]), d["months"]
        matured = date.today() >= add_months(start, months)
        val = fd_value(d["principal"], d["rate"], start, months)
        rows.append({"name": d.get("label", "FD"),
                     "sub": f"{d['rate']}%, matures {add_months(start, months)}"
                            + (" ✓ matured" if matured else ""),
                     "value": val, "prev": val})
    if rows:
        sections.append({"title": "Fixed Deposits", "bucket": "Fixed Deposits", "rows": rows})

    rows = []
    fx = safe("USDINR", lambda: quote("INR=X"))
    for key, fut, label in (("gold", "GC=F", "Gold"), ("silver", "SI=F", "Silver")):
        a = h.get(key)
        if not a or not fx:
            continue
        q = safe(label, lambda fut=fut: quote(fut))
        if not q:
            continue
        prem = a.get("premium_pct", 0)
        rate = metal_inr_per_g(q[0], fx[0], prem)
        prev_rate = metal_inr_per_g(q[1], fx[1], prem)
        grams, cost = a["grams"], a.get("cost_per_g")
        rows.append({"name": label, "sub": f"{prem}% over spot", "qty": grams,
                     "price": rate, "value": grams * rate, "prev": grams * prev_rate,
                     "gain": (rate / cost - 1) * 100 if cost else None})
    if rows:
        sections.append({"title": "Gold & Silver", "bucket": "Metals", "unit": "grams",
                         "rows": rows})

    for s in sections:
        s["total"] = sum(r["value"] for r in s["rows"])
        s["prev_total"] = sum(r["prev"] for r in s["rows"])
    return sections, nav_dates


# ---------------------------------------------------------------- render

COLORS = {"Stocks": "#3b6fd4", "Mutual Funds": "#12a594", "Retirement": "#8b5cf6",
          "Fixed Deposits": "#5b7083", "Metals": "#d99a2b"}
MARKET = {"Stocks", "Mutual Funds", "Metals"}  # the only buckets a day change means anything for

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f4f5f7;color:#1a1d21;
     font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding-block:28px;padding-left:16px;padding-right:16px}
h1{font-size:15px;font-weight:600;color:#5b6570;margin:0 0 4px;letter-spacing:.02em}
.net{font-size:38px;font-weight:650;letter-spacing:-.02em;margin:0}
.sub{color:#5b6570;margin:6px 0 0}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:20px;margin-bottom:16px}
.alloc{display:flex;gap:28px;align-items:center;flex-wrap:wrap}
.donut{position:relative;width:150px;height:150px;border-radius:50%;flex:0 0 auto}
.hole{position:absolute;inset:26px;background:#fff;border-radius:50%}
.legend{flex:1 1 260px;min-width:0}
.leg{display:flex;align-items:center;gap:9px;padding:4px 0}
.dot{width:10px;height:10px;border-radius:3px;flex:0 0 auto}
.leg b{margin-left:auto;font-variant-numeric:tabular-nums}
.leg span.p{color:#5b6570;width:46px;text-align:right;font-variant-numeric:tabular-nums}
h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;
   color:#5b6570;margin:0 0 12px}
.scroll{overflow-x:auto;margin:0 -4px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#7a838d;
   text-align:right;font-weight:600;padding:0 4px 8px}
th:first-child{text-align:left}
td{padding:9px 4px;border-top:1px solid #eef0f2;text-align:right;white-space:nowrap}
td:first-child{text-align:left;white-space:normal;min-width:150px}
.nm{font-weight:500}
.sb{color:#7a838d;font-size:12px}
tfoot td{border-top:1px solid #d8dce0;font-weight:600}
.up{color:#12855f}.dn{color:#c0392b}.mut{color:#7a838d}
.err{background:#fff6f5;border-color:#f3c9c4}
.err li{margin:3px 0;font-family:ui-monospace,monospace;font-size:12px}
footer{color:#7a838d;font-size:12px;margin-top:22px}
footer div{margin:2px 0}
"""


def cls(x):
    return "up" if x > 0 else ("dn" if x < 0 else "mut")


def render(sections, nav_dates):
    net = sum(s["total"] for s in sections)
    mkt = sum(s["total"] for s in sections if s["bucket"] in MARKET)
    mkt_prev = sum(s["prev_total"] for s in sections if s["bucket"] in MARKET)
    day = mkt - mkt_prev
    day_pct = (day / mkt_prev * 100) if mkt_prev else 0

    stops, legend, at = [], [], 0.0
    for s in sections:
        share = s["total"] / net * 100 if net else 0
        c = COLORS[s["bucket"]]
        stops.append(f"{c} {at:.3f}% {at + share:.3f}%")
        legend.append(
            f'<div class="leg"><i class="dot" style="background:{c}"></i>'
            f'{s["title"]}<b>&#8377;{fmt_inr(s["total"])}</b>'
            f'<span class="p">{share:.1f}%</span></div>')
        at += share

    body = []
    for s in sections:
        priced = "unit" in s
        head = (f'<tr><th>Holding</th><th>{s["unit"].title()}</th><th>Price</th>'
                f'<th>Value</th><th>Day</th><th>Gain</th></tr>' if priced
                else '<tr><th>Holding</th><th>Value</th></tr>')
        trs = []
        for r in s["rows"]:
            if priced:
                d = (r["value"] / r["prev"] - 1) * 100 if r["prev"] else 0
                g = (f'<span class="{cls(r["gain"])}">{pct(r["gain"])}</span>'
                     if r["gain"] is not None else '<span class="mut">&mdash;</span>')
                trs.append(
                    f'<tr><td><div class="nm">{r["name"]}</div>'
                    f'<div class="sb">{r["sub"]}</div></td>'
                    f'<td>{r["qty"]:g}</td><td>{r["price"]:,.2f}</td>'
                    f'<td>&#8377;{fmt_inr(r["value"])}</td>'
                    f'<td class="{cls(d)}">{pct(d)}</td><td>{g}</td></tr>')
            else:
                trs.append(
                    f'<tr><td><div class="nm">{r["name"]}</div>'
                    f'<div class="sb">{r["sub"]}</div></td>'
                    f'<td>&#8377;{fmt_inr(r["value"])}</td></tr>')
        span = 2 if priced else 0  # blanks between 'Subtotal' and the Value column
        body.append(
            f'<section class="card"><h2>{s["title"]}</h2><div class="scroll"><table>'
            f'<thead>{head}</thead><tbody>{"".join(trs)}</tbody>'
            f'<tfoot><tr><td>Subtotal</td>{"<td></td>" * span}'
            f'<td>&#8377;{fmt_inr(s["total"])}</td>'
            f'{"<td></td><td></td>" if priced else ""}</tr></tfoot>'
            f'</table></div></section>')

    err = (f'<section class="card err"><h2>Could not price</h2><ul>'
           f'{"".join(f"<li>{e}</li>" for e in ERRORS)}</ul></section>') if ERRORS else ""
    navs = "".join(f"<div>{n} &mdash; NAV {d}</div>" for n, d in nav_dates)

    return f"""<title>Portfolio</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style>
<div class="wrap">
<h1>NET WORTH</h1>
<p class="net">&#8377;{fmt_inr(net)}</p>
<p class="sub">Market assets <span class="{cls(day)}">{pct(day_pct)}
 ({'+' if day >= 0 else '-'}&#8377;{fmt_inr(abs(day))})</span> today
 &middot; &#8377;{fmt_inr(mkt)} of &#8377;{fmt_inr(net)} is market-priced</p>
<section class="card alloc" style="margin-top:20px">
  <div class="donut" style="background:conic-gradient({','.join(stops)})"><div class="hole"></div></div>
  <div class="legend">{''.join(legend)}</div>
</section>
{err}{''.join(body)}
<footer><div>Generated {datetime.now():%d %b %Y, %H:%M}</div>{navs}
<div>PPF/EPF accrued from the balance you entered; NPS and FDs are not market-priced.</div>
</footer></div>"""


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--holdings", default="holdings.yaml")
    p.add_argument("--out", default="portfolio.html")
    p.add_argument("--find", metavar="NAME", help="look up mutual fund scheme codes")
    a = p.parse_args()

    if a.find:
        r = requests.get(MFSEARCH.format(urllib.parse.quote(a.find)), headers=UA, timeout=20)
        r.raise_for_status()
        hits = r.json()
        for m in hits:
            print(f'{m["schemeCode"]:>8}  {m["schemeName"]}')
        if not hits:
            print("no match")
        return 0

    sections, nav_dates = build(load_holdings(a.holdings))
    for e in ERRORS:
        print(f"  ! {e}", file=sys.stderr)
    if not sections:
        print("Nothing could be priced." if ERRORS
              else f"Nothing to show. Add holdings to {a.holdings}.", file=sys.stderr)
        return 1
    with open(a.out, "w") as f:
        f.write(render(sections, nav_dates))
    print(f"{sum(len(s['rows']) for s in sections)} holdings priced -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
