"""
nous_usage_server.py — self-hosted HTTP JSON API for Nous Portal usage.

Serves live Nous Portal subscription spend + local Hermes model usage as
JSON, so you can point a dashboard, status page, or e-Paper display at it.
This is the data mouthpiece: it reads the Portal account API (spend/cap)
plus local session token usage and returns one snapshot.

Endpoints:
    GET /usage  -> JSON snapshot (spend, cap, %, days left, on-pace,
                   token totals, per-model breakdown)
    GET /       -> tiny HTML status page (browser check)

Run:
    python3 nous_usage_server.py --port 8765

Zero dependencies — pure Python 3 stdlib (http.server + urllib + sqlite3).
"""

import argparse
import datetime as dt
import json
import os
import sqlite3
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DB = HERMES_HOME / "state.db"
AUTH_JSON = HERMES_HOME / "auth.json"
PORTAL_BASE = "https://portal.nousresearch.com"

# Fallback monthly charge used only when the Portal payload reports neither
# a spend cap nor monthly credits (kept configurable, not hardcoded per user).
DEFAULT_MONTHLY_CHARGE = 20.0

# Cache the Portal account fetch briefly so polling clients don't hammer it.
_CACHE = {"ts": 0.0, "payload": None}
CACHE_SECONDS = 300


def _as_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_nous_credential():
    """Return (portal_base_url, access_token) from auth.json, or None."""
    try:
        data = json.loads(AUTH_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    prov = (data.get("providers") or {}).get("nous") or {}
    tok = prov.get("access_token")
    base = prov.get("portal_base_url") or PORTAL_BASE
    if not tok:
        pool = (data.get("credential_pool") or {}).get("nous") or []
        if pool:
            e = pool[0]
            tok = e.get("access_token") or tok
            base = e.get("portal_base_url") or base
    if not tok:
        return None
    return base.rstrip("/"), tok


def fetch_portal_account():
    """Fetch {base}/api/oauth/account with a short cache."""
    now = dt.datetime.now().timestamp()
    if _CACHE["payload"] and now - _CACHE["ts"] < CACHE_SECONDS:
        return _CACHE["payload"]
    cred = load_nous_credential()
    if cred is None:
        return {"error": "no nous credential"}
    base, token = cred
    url = f"{base}/api/oauth/account"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "nous-usage-server/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        _CACHE["ts"] = now
        _CACHE["payload"] = payload
        return payload
    except Exception as exc:  # noqa: BLE001
        return {"error": f"portal account api failed: {exc}"}


def load_local_usage(days: Optional[int] = 30, cutoff_ts: Optional[float] = None) -> dict:
    """Aggregate per-model token usage + cost from the session store."""
    if not STATE_DB.exists():
        return {"rows": [], "totals": {}, "error": f"session store not found: {STATE_DB}"}
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"rows": [], "totals": {}, "error": f"cannot open session store: {exc}"}
    if cutoff_ts is None and days:
        cutoff_ts = (dt.datetime.now() - dt.timedelta(days=days)).timestamp()
    sql = """
        SELECT model, billing_provider,
               SUM(api_call_count)   AS api_calls,
               SUM(input_tokens)     AS input_tokens,
               SUM(output_tokens)    AS output_tokens,
               SUM(reasoning_tokens) AS reasoning_tokens,
               SUM(cache_read_tokens)  AS cache_read,
               SUM(cache_write_tokens) AS cache_write,
               SUM(estimated_cost_usd) AS estimated_cost_usd,
               SUM(actual_cost_usd)    AS actual_cost_usd,
               MAX(last_seen)          AS last_seen
        FROM session_model_usage
    """
    params = []
    if cutoff_ts is not None:
        sql += " WHERE last_seen >= ?"
        params.append(cutoff_ts)
    sql += " GROUP BY model, billing_provider ORDER BY SUM(estimated_cost_usd) DESC, model"
    try:
        cur = con.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
    except sqlite3.Error as exc:
        return {"rows": [], "totals": {}, "error": f"session store query failed: {exc}"}
    totals = {
        "api_calls": sum(r["api_calls"] or 0 for r in rows),
        "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
        "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
        "reasoning_tokens": sum(r["reasoning_tokens"] or 0 for r in rows),
        "cache_read": sum(r["cache_read"] or 0 for r in rows),
        "cache_write": sum(r["cache_write"] or 0 for r in rows),
        "estimated_cost_usd": sum(r["estimated_cost_usd"] or 0 for r in rows),
        "actual_cost_usd": sum(r["actual_cost_usd"] or 0 for r in rows),
    }
    return {"rows": rows, "totals": totals, "error": None}


def parse_period_end(iso):
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def build_snapshot() -> dict:
    """Compose the JSON snapshot (spend, cap, pacing, token totals, models)."""
    account = fetch_portal_account()
    usage = load_local_usage(30)

    sub = account.get("subscription") or {}
    access = account.get("paid_service_access") or {}
    spend = _as_float(access.get("member_spend_usd"))
    # Cap: prefer the Portal-reported spend cap; fall back to monthly credits
    # (the effective cap), then the raw monthly charge.
    cap = _as_float(access.get("member_spend_cap_usd"))
    period_end = parse_period_end(sub.get("current_period_end"))
    monthly_credits = _as_float(sub.get("monthly_credits"))
    charge = _as_float(sub.get("monthly_charge")) or DEFAULT_MONTHLY_CHARGE

    cap_eff = cap or monthly_credits or charge
    pct = (spend / cap_eff * 100.0) if (spend is not None and cap_eff) else 0.0

    days_left = None
    if period_end is not None:
        days_left = max(0, (period_end - dt.datetime.now(period_end.tzinfo)).days)

    # Burn-rate projection: spend so far / days elapsed in period * total days.
    on_pace = None
    if spend is not None and period_end is not None:
        now = dt.datetime.now(period_end.tzinfo)
        period_start = period_end.replace(day=1)
        total_days = max(1, (period_end - period_start).days)
        elapsed = max(0, (now - period_start).days) + 1
        on_pace = spend / elapsed * total_days

    total_tokens = sum([
        usage.get("totals", {}).get("input_tokens") or 0,
        usage.get("totals", {}).get("output_tokens") or 0,
        usage.get("totals", {}).get("cache_read") or 0,
        usage.get("totals", {}).get("cache_write") or 0,
    ])

    today_usage = load_local_usage(None, cutoff_ts=dt.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    today_tokens = sum([
        today_usage.get("totals", {}).get("input_tokens") or 0,
        today_usage.get("totals", {}).get("output_tokens") or 0,
        today_usage.get("totals", {}).get("cache_read") or 0,
        today_usage.get("totals", {}).get("cache_write") or 0,
    ])

    total_spend = sum(r.get("estimated_cost_usd") or 0 for r in usage.get("rows", []))
    models = []
    for r in usage.get("rows", []):
        m_tokens = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) \
            + (r.get("cache_read") or 0) + (r.get("cache_write") or 0)
        m_spend = r.get("estimated_cost_usd") or 0
        m_pct = (m_spend / total_spend * 100.0) if total_spend else 0.0
        models.append({
            "model": r.get("model") or "unknown",
            "spend_usd": m_spend,
            "tokens": m_tokens,
            "pct": m_pct,
        })

    return {
        "spend_usd": spend if spend is not None else 0.0,
        "cap_usd": cap_eff,
        "pct": pct,
        "days_left": days_left,
        "on_pace_usd": on_pace,
        "period_end": sub.get("current_period_end"),
        "total_tokens": total_tokens,
        "today_tokens": today_tokens,
        "models": models,
        "error": account.get("error"),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/usage"):
            try:
                snap = build_snapshot()
                body = json.dumps(snap).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}).encode("utf-8"),
                           "application/json; charset=utf-8")
        else:
            snap = build_snapshot()
            spend = snap.get("spend_usd")
            pct = snap.get("pct")
            body = (
                "<html><body style='font-family:monospace;padding:2rem'>"
                "<h1>Nous usage server</h1>"
                f"<p style='font-size:2rem'>spend: <b>${spend:.2f}</b> "
                f"({pct:.1f}% of cap)</p>"
                f"<p>days left: {snap.get('days_left')} · "
                f"on pace: ${snap.get('on_pace_usd'):.0f}</p>"
                f"<p><small>error: {snap.get('error')}</small></p>"
                "</body></html>"
            ).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[nous-usage] server listening on 0.0.0.0:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[nous-usage] bye")
        server.server_close()


if __name__ == "__main__":
    main()
