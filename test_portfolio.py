#!/usr/bin/env python3
"""Offline checks for the money math. No network: run anywhere, anytime."""
from datetime import date

from portfolio import (accrue, add_months, as_date, fd_value, fmt_inr,
                       metal_inr_per_g)

# Indian digit grouping
assert fmt_inr(0) == "0"
assert fmt_inr(999) == "999"
assert fmt_inr(1000) == "1,000"
assert fmt_inr(123456) == "1,23,456"
assert fmt_inr(1234567) == "12,34,567"          # 12 lakh
assert fmt_inr(12345678) == "1,23,45,678"       # 1.23 crore
assert fmt_inr(-1234567) == "-12,34,567"
assert fmt_inr(1234.6) == "1,235"               # rounds, not truncates

# Month arithmetic clamps to a real day of month
assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
assert add_months(date(2025, 1, 10), 24) == date(2027, 1, 10)
assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)

# FD: quarterly compounding, and frozen once matured
start, rate, months = date(2025, 1, 10), 7.25, 24
at_maturity = fd_value(100000, rate, start, months, today=date(2027, 1, 10))
assert abs(at_maturity - 100000 * (1 + rate / 400) ** 8) < 0.01, at_maturity
assert abs(at_maturity - 115453.95) < 0.01, at_maturity   # 8 quarters, exactly
later = fd_value(100000, rate, start, months, today=date(2030, 1, 1))
assert later == at_maturity, "must not keep growing past maturity"
assert fd_value(100000, rate, start, months, today=start) == 100000
assert fd_value(100000, rate, start, months, today=date(2020, 1, 1)) == 100000

# PPF/EPF: annual compounding over exactly one year
assert abs(accrue(300000, date(2025, 1, 1), 7.1, today=date(2026, 1, 1)) - 321300) < 200
assert accrue(300000, date(2025, 1, 1), 7.1, today=date(2025, 1, 1)) == 300000
assert accrue(300000, date(2025, 1, 1), None) == 300000, "no rate -> balance as entered"

# Metals: the hand-checked figure from the live probe
#   4408.9 USD/oz x 95.55 INR/USD / 31.1035 g/oz = 13,543 INR/g international
#   x 1.06 premium                               = 14,356 INR/g local
assert abs(metal_inr_per_g(4408.9, 95.55, 0) - 13543) < 5
assert abs(metal_inr_per_g(4408.9, 95.55, 6.0) - 14356) < 5
assert metal_inr_per_g(100, 90, 0) < metal_inr_per_g(100, 90, 10)

# YAML hands us a date already; a quoted one arrives as str
assert as_date(date(2026, 3, 31)) == date(2026, 3, 31)
assert as_date("2026-03-31") == date(2026, 3, 31)

# Allocation shares over the committed fixture sum to 100
import yaml
h = yaml.safe_load(open("holdings.example.yaml"))
totals = [h["epf"]["balance"], h["ppf"]["balance"], h["nps"]["balance"],
          sum(d["principal"] for d in h["fd"]),
          h["gold"]["grams"] * h["gold"]["cost_per_g"]]
net = sum(totals)
assert abs(sum(t / net * 100 for t in totals) - 100) < 1e-9

print("all checks pass")
