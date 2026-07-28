"""Money handling. Integer cents end-to-end for cap WRITES; Decimal cents for
usage READS (the gateway's period_to_date_spend can be FRACTIONAL cents, e.g.
"123.5"). Floats never touch money - float round-trips put 0.05 on 6 cents, a
money bug this repo has already had once.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class AmountError(Exception):
    pass


def dollars_to_cents(amount):
    """'50', '50.5', '50.05' -> '5000'/'5050'/'5005'. Raises AmountError on
    anything that is not a plain non-negative dollar figure with at most two
    decimal places (never rounds money)."""
    amount = (amount or "").strip()
    if not amount or amount.strip(".") == "" or amount.count(".") > 1 \
            or any(c not in "0123456789." for c in amount):
        raise AmountError("amount must be a plain dollar figure, e.g. 50 or 50.00")
    dollars, _, frac = amount.partition(".")
    if len(frac) > 2:
        raise AmountError("amount has more than 2 decimal places")
    cents = int(dollars or "0") * 100 + int((frac + "00")[:2])
    if cents <= 0:
        raise AmountError("amount must be greater than zero")
    if len(str(cents)) > 18:
        raise AmountError("amount is too large")
    return str(cents)


def cents_to_dollars(cents):
    """'5005' -> '$50.05' for display. Anything non-numeric renders verbatim
    (defensive: the value comes from the gateway API)."""
    s = str(cents)
    if not s.isdigit():
        return s
    return "$%d.%02d" % (int(s) // 100, int(s) % 100)


# ---------------------------------------------------------------- usage math
# The /spend_limits/effective API reports amounts as cents STRINGS; spend may
# carry a fractional part. All comparisons run in Decimal; rounding happens
# only at DISPLAY time (never in a cap comparison).


def parse_cents(value):
    """Decimal cents from an API string (may be fractional, e.g. '123.5').
    Returns None for anything unparseable / negative / non-finite - callers
    render a '?' rather than crash on a surprising gateway value."""
    if value is None:
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def cents_str_to_display(value):
    """Cents string (possibly fractional) -> '$12.34' display string.
    Rounds HALF_UP to whole cents for display only."""
    d = parse_cents(value)
    if d is None:
        return str(value)
    dollars = (d / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "$%s" % dollars


def percent_used(spend_cents, cap_cents):
    """spend/cap as a one-decimal percentage, Decimal end-to-end.

    Returns a dict for the progress-bar renderers, or None when no meaningful
    percentage exists (no/zero cap, unparseable spend):
      display - '42.5%' (display capped at '>999%')
      width   - int 0..100 for the bar fill (CSS width class)
      cls     - 'ok' (<70) / 'warn' (70-90) / 'danger' (>=90)
      sort    - UNCAPPED one-decimal value as a string, for numeric sort keys
                (width ties every row above 100%; this does not)
    """
    spend = spend_cents if isinstance(spend_cents, Decimal) else parse_cents(spend_cents)
    cap = cap_cents if isinstance(cap_cents, Decimal) else parse_cents(cap_cents)
    if spend is None or cap is None or cap <= 0:
        return None
    pct = (spend / cap * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    display = ">999%" if pct > 999 else "%s%%" % pct
    width = int(min(pct, Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
    if pct < 70:
        cls = "ok"
    elif pct < 90:
        cls = "warn"
    else:
        cls = "danger"
    return {"display": display, "width": max(0, min(width, 100)), "cls": cls,
            "sort": str(pct)}
