"""Tests for nous_usage: subscription parsing, cost totals and formatting.

Everything runs against fixtures and a throwaway SQLite store - no live
Portal credentials or real Hermes DB involved.
"""

import json
import sqlite3

import pytest

import nous_usage
from nous_usage import (
    _as_float,
    fmt_tokens,
    fmt_usd,
    load_local_usage,
    load_nous_credential,
    parse_period_end,
    parse_subscription,
)


# --------------------------------------------------------------------- #
#  parse_subscription
# --------------------------------------------------------------------- #
FULL_PAYLOAD = {
    "subscription": {
        "plan": "standard",
        "tier": "pro",
        "monthly_charge": "20.00",
        "current_period_end": "2026-10-01T00:00:00Z",
        "credits_remaining": "12.5",
        "rollover_credits": "3",
    },
    "paid_service_access": {
        "member_spend_usd": "7.50",
        "member_spend_cap_usd": "22.00",
        "member_spend_cap_remaining_usd": "14.50",
        "member_spend_cap_exceeded": False,
        "has_active_subscription": True,
        "active_subscription_is_paid": True,
        "purchased_credits_remaining": "5",
        "total_usable_credits": "20.5",
    },
}


def test_parse_subscription_flattens_and_coerces_numbers():
    s = parse_subscription(FULL_PAYLOAD)
    assert s["plan"] == "standard"
    assert s["tier"] == "pro"
    assert s["monthly_charge"] == 20.0
    assert s["credits_remaining"] == 12.5
    assert s["rollover_credits"] == 3.0
    assert s["spend_usd"] == 7.5
    assert s["cap_usd"] == 22.0
    assert s["cap_remaining_usd"] == 14.5
    assert s["purchased_credits"] == 5.0
    assert s["total_usable_credits"] == 20.5
    assert s["cap_exceeded"] is False
    assert s["active_subscription"] is True
    assert s["subscription_is_paid"] is True


def test_parse_subscription_error_passthrough():
    assert parse_subscription({"_error": "boom"}) == {"error": "boom"}


def test_parse_subscription_missing_sections_yield_nones():
    s = parse_subscription({})
    assert s["plan"] is None
    assert s["spend_usd"] is None
    assert s["credits_remaining"] is None
    assert s["cap_exceeded"] is None


def test_parse_subscription_ignores_unparseable_numbers():
    payload = {"subscription": {"monthly_charge": "not-a-number"}}
    assert parse_subscription(payload)["monthly_charge"] is None


# --------------------------------------------------------------------- #
#  load_nous_credential
# --------------------------------------------------------------------- #
def _write_auth(tmp_path, monkeypatch, data):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(data))
    monkeypatch.setattr(nous_usage, "AUTH_JSON", auth)
    return auth


def test_credential_from_oauth_provider(tmp_path, monkeypatch):
    _write_auth(tmp_path, monkeypatch, {
        "providers": {"nous": {"access_token": "tok123",
                               "portal_base_url": "https://portal.example.com/"}},
    })
    assert load_nous_credential() == ("https://portal.example.com", "tok123")


def test_credential_falls_back_to_pool(tmp_path, monkeypatch):
    _write_auth(tmp_path, monkeypatch, {
        "credential_pool": {"nous": [{"access_token": "pooltok"}]},
    })
    base, tok = load_nous_credential()
    assert tok == "pooltok"
    assert base == "https://portal.nousresearch.com"


def test_credential_missing_or_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(nous_usage, "AUTH_JSON", tmp_path / "nope.json")
    assert load_nous_credential() is None

    auth = tmp_path / "auth.json"
    auth.write_text("{not json")
    monkeypatch.setattr(nous_usage, "AUTH_JSON", auth)
    assert load_nous_credential() is None


# --------------------------------------------------------------------- #
#  load_local_usage (fixture SQLite store)
# --------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE session_model_usage (
    model TEXT,
    billing_provider TEXT,
    api_call_count INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    last_seen REAL
)
"""


def _make_db(tmp_path, monkeypatch, rows):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(SCHEMA)
    con.executemany(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    con.commit()
    con.close()
    monkeypatch.setattr(nous_usage, "STATE_DB", db)
    return db


def _row(model, cost, seen, calls=1, in_tok=100, out_tok=50):
    return (model, "nous", calls, in_tok, out_tok, 0, 0, 0, cost, cost, seen)


def test_usage_aggregates_rows_and_totals(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch, [
        _row("model-a", 1.50, 2000, calls=2, in_tok=1000, out_tok=500),
        _row("model-a", 0.50, 3000, calls=1, in_tok=200, out_tok=100),
        _row("model-b", 0.25, 1000),
    ])
    res = load_local_usage(days=None)
    assert res["error"] is None
    assert len(res["rows"]) == 2

    a = next(r for r in res["rows"] if r["model"] == "model-a")
    assert a["api_calls"] == 3
    assert a["input_tokens"] == 1200
    assert a["output_tokens"] == 600
    assert a["estimated_cost_usd"] == pytest.approx(2.0)
    assert a["last_seen"] == 3000

    t = res["totals"]
    assert t["api_calls"] == 4
    assert t["estimated_cost_usd"] == pytest.approx(2.25)
    assert t["actual_cost_usd"] == pytest.approx(2.25)


def test_usage_respects_cutoff(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch, [
        _row("old", 1.0, 1000),
        _row("new", 2.0, 5000),
    ])
    res = load_local_usage(days=None, cutoff_ts=4000)
    assert [r["model"] for r in res["rows"]] == ["new"]
    assert res["totals"]["estimated_cost_usd"] == pytest.approx(2.0)


def test_usage_missing_store_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(nous_usage, "STATE_DB", tmp_path / "missing.db")
    res = load_local_usage(days=30)
    assert res["rows"] == []
    assert "not found" in res["error"]


# --------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    (None, None), ("3.5", 3.5), (7, 7.0), ("x", None),
])
def test_as_float(value, expected):
    assert _as_float(value) == expected


@pytest.mark.parametrize("n,expected", [
    (0, "0"), (123, "123"), (1500, "1.5k"), (2_500_000, "2.50M"), (None, "0"),
])
def test_fmt_tokens(n, expected):
    assert fmt_tokens(n) == expected


def test_fmt_usd():
    assert fmt_usd(12.5) == "$12.50"
    assert fmt_usd(0) == "$0.00"
    assert fmt_usd(0.0042) == "$0.0042"


def test_parse_period_end_formats_iso():
    assert parse_period_end("") == "—"
    assert parse_period_end("garbage") == "garbage"
    out = parse_period_end("2026-10-01T12:00:00Z")
    assert out.endswith("2026") and "Oct" in out
