# portfolio_analyzer

View all your investments/portfolio at a single page, and get fine tuned AI and ML based recommendations as per the latest trends

## Usage

```sh
cp holdings.example.yaml holdings.yaml   # then edit in your own numbers
python3 portfolio.py                     # writes portfolio.html
google-chrome portfolio.html             # or: firefox portfolio.html
```

Run it from this directory: pyenv resolves `python3` per-directory, and the
interpreter with `requests` and `PyYAML` is the one selected here.

`xdg-open portfolio.html` works only if `text/html` is associated with a browser.
Check with `xdg-mime query default text/html`; if it names something odd (a Slack
install will claim it), fix with `xdg-mime default google-chrome.desktop text/html`.

No logins, no API keys, no server. Only prices are fetched; your holdings never
leave the machine. `holdings.yaml` and `portfolio.html` are gitignored.

To look up a mutual fund's scheme code for the YAML:

```sh
python3 portfolio.py --find "parag parikh flexi"
```

Checks: `python3 test_portfolio.py` (offline, no network).

## What gets priced, and what doesn't

| Asset | Source |
| --- | --- |
| Stocks | Yahoo Finance — NSE `.NS`, BSE `.BO` |
| Mutual funds | `api.mfapi.in` (AMFI mirror; AMFI direct is blocked on the corp network) |
| Gold, silver | `GC=F` / `SI=F` × `INR=X` ÷ 31.1035, times your `premium_pct` |
| PPF, EPF | Compounded forward from the balance and date you enter |
| Fixed deposits | Quarterly compounding, frozen at maturity |
| NPS | Balance as entered — NPS Trust's NAV endpoint is blocked on the corp network |

`premium_pct` is a calibration knob: international spot sits below Indian retail
because of import duty and GST. Set it once so the per-gram rate matches what your
jeweller quotes, then leave it.

Day change is shown for market-priced assets only — a day change on a PPF balance
would be meaningless. Gain% appears only where there's a real cost basis, so PPF,
EPF, NPS and FDs show value alone rather than a made-up number.

Dependencies: `requests` and `PyYAML`, both already present. Nothing to install.

## News-based recommendations

```sh
python3 advisor.py --plan      # show the 30 + 15 Marketaux calls, spends nothing
python3 advisor.py             # fetch news, ask Claude, print table + advice.json
```

Needs `.env` (gitignored) with `MARKETAUX_TOKEN=...` and `ANTHROPIC_API_KEY=...`,
plus `pip install anthropic`. Both keys are checked before any quota is spent.

Budgets are flags: `--calls 30 --discovery 15 --per-call 3`. Portfolio calls go
one symbol each, largest position first; spare calls buy extra pages. Discovery
calls page through India-listed news filtered to strong positive/negative
sentiment, and the most extreme names become buy/short candidates. Marketaux
allows 30 requests a minute, so a full run waits about a minute between the two
batches. Results are cached for 6 hours, so a rerun is free.

Price levels (add below, sell above, stop) are anchored to Yahoo 52-week
high/low and 50/200-day averages, and any level on the wrong side of the live
price is flagged rather than shown. Checks: `python3 test_advisor.py`.

Output is generated from news sentiment. It is not financial advice.
