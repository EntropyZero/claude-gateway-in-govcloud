"""Money handling: integer cents for cap writes, Decimal cents for usage
reads (period_to_date_spend may be FRACTIONAL cents), display + percent
helpers. Floats never touch money."""

from decimal import Decimal

import pytest

from portal.money import (AmountError, cents_str_to_display, cents_to_dollars,
                          dollars_to_cents, parse_cents, percent_used)


# ------------------------------------------------------------- dollars/cents


def test_dollars_to_cents_exact():
    assert dollars_to_cents("50") == "5000"
    assert dollars_to_cents("50.5") == "5050"
    assert dollars_to_cents("0.05") == "5"     # the historical float bug
    assert dollars_to_cents("1234.56") == "123456"
    assert dollars_to_cents("99999999.99") == "9999999999"


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3", "0.001", "-5", "0", "0.00", "."])
def test_dollars_to_cents_rejects(bad):
    with pytest.raises(AmountError):
        dollars_to_cents(bad)


def test_cents_to_dollars():
    assert cents_to_dollars("5005") == "$50.05"
    assert cents_to_dollars("5") == "$0.05"
    assert cents_to_dollars("not-a-number") == "not-a-number"  # defensive


# ------------------------------------------------------------- parse_cents


def test_parse_cents_integer_and_fractional():
    assert parse_cents("5000") == Decimal("5000")
    assert parse_cents("123.5") == Decimal("123.5")   # fractional cents, live-observed
    assert parse_cents("0.75") == Decimal("0.75")
    assert parse_cents(0) == Decimal(0)


def test_parse_cents_rejects_garbage():
    assert parse_cents(None) is None
    assert parse_cents("abc") is None
    assert parse_cents("-5") is None
    assert parse_cents("NaN") is None
    assert parse_cents("Infinity") is None


# ------------------------------------------------------------- display


def test_cents_str_to_display():
    assert cents_str_to_display("5000") == "$50.00"
    assert cents_str_to_display("5") == "$0.05"
    # Fractional cents round HALF_UP at DISPLAY time only.
    assert cents_str_to_display("123.5") == "$1.24"
    assert cents_str_to_display("0.75") == "$0.01"
    assert cents_str_to_display("12345.5") == "$123.46"
    # Unparseable values render verbatim, never crash.
    assert cents_str_to_display("bogus") == "bogus"


# ------------------------------------------------------------- percent_used


def test_percent_used_basic():
    pct = percent_used(Decimal("2500"), Decimal("10000"))
    assert pct["display"] == "25.0%"
    assert pct["width"] == 25
    assert pct["cls"] == "ok"


def test_percent_used_accepts_strings_and_fractional_cents():
    pct = percent_used("123.5", "5000")
    assert pct["display"] == "2.5%"      # Decimal math, one decimal HALF_UP
    assert pct["cls"] == "ok"


def test_percent_used_class_boundaries():
    assert percent_used("6950", "10000")["cls"] == "ok"      # 69.5%
    assert percent_used("7000", "10000")["cls"] == "warn"    # 70.0%
    assert percent_used("8940", "10000")["cls"] == "warn"    # 89.4%
    # Classification follows the DISPLAYED (rounded) value: 89.99 -> "90.0%".
    assert percent_used("8999", "10000")["cls"] == "danger"
    assert percent_used("9000", "10000")["cls"] == "danger"  # 90.0%
    assert percent_used("15000", "10000")["cls"] == "danger"


def test_percent_used_width_clamped_to_100():
    pct = percent_used("15000", "10000")
    assert pct["display"] == "150.0%"
    assert pct["width"] == 100


def test_percent_used_display_capped_at_999():
    pct = percent_used("1000000", "10")     # 10,000,000%
    assert pct["display"] == ">999%"
    assert pct["width"] == 100


def test_percent_used_sort_key_is_uncapped():
    # width ties every row above 100%; the sort key must not (150% vs 999%
    # must order correctly in the client-side table sort).
    assert percent_used("15000", "10000")["sort"] == "150.0"
    assert percent_used("1000000", "10")["sort"] == "10000000.0"
    assert percent_used("2500", "10000")["sort"] == "25.0"


def test_percent_used_no_cap_or_bad_input_is_none():
    assert percent_used("100", None) is None
    assert percent_used("100", "0") is None       # zero cap: no meaningful %
    assert percent_used(None, "100") is None
    assert percent_used("junk", "100") is None
