#!/usr/bin/env python3
"""
nous-usage — terminal usage dashboard for the Nous Portal $20/mo subscription.

Shows:
  • Live subscription state  (plan, credits remaining, spend this period, period end)
  • Per-model token usage    (input / output / reasoning / cached) with estimated cost
  • Time-window selection    (today / 7d / 30d / all), with live --watch refresh

Zero dependencies — pure Python 3 stdlib + ANSI escape codes.

Usage:
  nous-usage                    one-shot dashboard (last 30 days)
  nous-usage --days 7           last 7 days
  nous-usage --watch            refresh every 10s (press q or Ctrl-C to quit)
  nous-usage --watch --every 5  refresh every 5s
  nous-usage --json             emit machine-readable JSON and exit
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DB = HERMES_HOME / "state.db"
AUTH_JSON = HERMES_HOME / "auth.json"

# --------------------------------------------------------------------------- #
#  ANSI helpers (color-aware; falls back to plain when not a TTY)
# --------------------------------------------------------------------------- #
class C:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def bold(self, s):    return self._w("1", s)
    def dim(self, s):     return self._w("2", s)
    def green(self, s):   return self._w("32", s)
    def yellow(self, s):  return self._w("33", s)
    def red(self, s):     return self._w("31", s)
    def cyan(self, s):    return self._w("36", s)
    def magenta(self, s): return self._w("35", s)


# --------------------------------------------------------------------------- #
#  Data layer — Portal account API
# --------------------------------------------------------------------------- #
def load_nous_credential() -> tuple[str, str] | None:
    """Return (portal_base_url, access_token) from auth.json, or None."""
    try:
        data = json.loads(AUTH_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # Prefer the OAuth provider entry, then the credential pool.
    prov = (data.get("providers") or {}).get("nous") or {}
    tok = prov.get("access_token")
    base = prov.get("portal_base_url") or "https://portal.nousresearch.com"
    if not tok:
        pool = (data.get("credential_pool") or {}).get("nous") or []
        if pool:
            e = pool[0]
            tok = e.get("access_token") or tok
            base = e.get("portal_base_url") or base
    if not tok:
        return None
    return base.rstrip("/"), tok


def fetch_portal_account() -> dict:
    """Fetch {base}/api/oauth/account and return the raw JSON payload."""
    cred = load_nous_credential()
    if cred is None:
        return {"_error": "No Nous Portal credential found in auth.json — run `hermes login --provider nous`."}
    base, token = cred
    url = f"{base}/api/oauth/account"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "nous-usage/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"Portal account API failed: {exc}"}


def parse_subscription(payload: dict) -> dict:
    """Flatten the account payload into a friendly summary dict."""
    if "_error" in payload:
        return {"error": payload["_error"]}

    sub = payload.get("subscription") or {}
    access = payload.get("paid_service_access") or {}

    plan = sub.get("plan")
    tier = sub.get("tier")
    monthly_charge = sub.get("monthly_charge")
    period_end = sub.get("current_period_end")
    credits_remaining = sub.get("credits_remaining")
    rollover = sub.get("rollover_credits")

    spend_usd = _as_float(access.get("member_spend_usd"))
    cap_usd = _as_float(access.get("member_spend_cap_usd"))
    cap_remaining = _as_float(access.get("member_spend_cap_remaining_usd"))
    cap_exceeded = access.get("member_spend_cap_exceeded")
    active_sub = access.get("has_active_subscription")
    sub_is_paid = access.get("active_subscription_is_paid")
    purchased = _as_float(access.get("purchased_credits_remaining"))
    total_usable = _as_float(access.get("total_usable_credits"))

    return {
        "plan": plan,
        "tier": tier,
        "monthly_charge": _as_float(monthly_charge),
        "period_end": period_end,
        "credits_remaining": _as_float(credits_remaining),
        "rollover_credits": _as_float(rollover),
        "purchased_credits": purchased,
        "total_usable_credits": total_usable,
        "spend_usd": spend_usd,
        "cap_usd": cap_usd,
        "cap_remaining_usd": cap_remaining,
        "cap_exceeded": cap_exceeded,
        "active_subscription": active_sub,
        "subscription_is_paid": sub_is_paid,
    }


# --------------------------------------------------------------------------- #
#  Data layer — local state.db (token usage)
# --------------------------------------------------------------------------- #
def load_local_usage(days: int | None, cutoff_ts: float | None = None) -> dict:
    """Aggregate per-model token usage + cost from the session store.

    Pass either `days` (integer look-back) or an explicit `cutoff_ts`
    (epoch seconds). If both, `cutoff_ts` wins.

    Returns {"rows": [...], "totals": {...}, "error": str|None}
    """
    if not STATE_DB.exists():
        return {"rows": [], "totals": {}, "error": f"session store not found: {STATE_DB}"}

    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"rows": [], "totals": {}, "error": f"cannot open session store: {exc}"}

    if cutoff_ts is None and days:
        cutoff_ts = (dt.datetime.now() - dt.timedelta(days=days)).timestamp()

    # Aggregate per model across all sessions / tasks, filtered by window.
    sql = """
        SELECT model,
               billing_provider,
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
    params: list = []
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


# --------------------------------------------------------------------------- #
#  Formatting helpers
# --------------------------------------------------------------------------- #
def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_tokens(n) -> str:
    """Human readable token count: 1.23M / 456.7k / 123."""
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return f"{int(n)}"


def fmt_usd(n, dp: int = 2) -> str:
    n = float(n or 0)
    if n and n < 0.01:
        return f"${n:.4f}"
    return f"${n:.{dp}f}"


def parse_period_end(iso: str) -> str:
    if not iso:
        return "—"
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return iso
    return d.strftime("%b %d, %Y")


# --------------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------------- #
def render_dashboard(usage: dict, account: dict, window_label: str, c: C) -> str:
    lines: list[str] = []
    W = 74
    sep = c.dim("─" * W)

    lines.append(c.bold(c.cyan("╔" + "═" * (W - 2) + "╗")))
    title = " NOUS USAGE "
    pad = (W - 2 - len(title)) // 2
    lines.append(c.bold(c.cyan("║")) + " " * pad + c.bold(title) + " " * (W - 2 - pad - len(title)) + c.bold(c.cyan("║")))
    lines.append(c.bold(c.cyan("╚" + "═" * (W - 2) + "╝")))

    # --- Headline: subscription-level spend against the monthly credit grant ---
    if not account.get("error"):
        spend = account.get("spend_usd")
        remain = account.get("credits_remaining")
        charge = account.get("monthly_charge")
        # Actual monthly credit grant (Plus grants $22 on a $20 sub).
        # Override with NOUS_SUB_GRANT env var if Portal changes the grant.
        try:
            grant = float(os.environ.get("NOUS_SUB_GRANT", "22.0"))
        except ValueError:
            grant = None
        if spend is not None:
            bucket = grant or charge or ((spend + remain) if remain is not None else None)
            spend_str = fmt_usd(spend)
            if bucket:
                pct = (spend / bucket * 100.0)
                color = c.green if pct < 50 else (c.yellow if pct < 80 else c.red)
                lines.append(f"  Subscription spend:  {c.bold(spend_str)} of {fmt_usd(bucket)}  ({pct:.1f}%)")
            else:
                lines.append(f"  Subscription spend:  {c.bold(spend_str)}")

    lines.append("")
    lines.append(c.bold(c.green("▌ Subscription")))
    lines.append(sep)
    if account.get("error"):
        lines.append(c.yellow("  " + account["error"]))
    else:
        plan = account.get("plan") or "Unknown"
        charge = account.get("monthly_charge")
        spend = account.get("spend_usd")
        remain = account.get("credits_remaining")
        purchased = account.get("purchased_credits")
        total_usable = account.get("total_usable_credits")
        period_end = parse_period_end(account.get("period_end"))
        active = account.get("active_subscription")

        plan_str = f"{plan}" + (f" (tier {account['tier']})" if account.get("tier") else "")
        charge_str = f"${charge:.0f}/mo" if charge else "—"
        spend_str = fmt_usd(spend) if spend is not None else "—"
        remain_str = fmt_usd(remain) if remain is not None else "—"
        purchased_str = fmt_usd(purchased) if purchased is not None else "—"
        total_str = fmt_usd(total_usable) if total_usable is not None else "—"

        lines.append(f"  Plan                {c.bold(plan_str):<28} Monthly       {c.bold(charge_str)}")
        status = "ACTIVE" if active else "inactive"
        status_col = c.green(status) if active else c.yellow(status)
        lines.append(f"  Subscription        {status_col:<28} Period end    {period_end}")
        lines.append(f"  Spent this period   {c.magenta(spend_str):<28} Credits left  {c.green(remain_str)}")
        if purchased is not None and total_usable is not None:
            lines.append(f"  Balance (usable)    {c.bold(total_str):<28} incl top-up   {c.green(purchased_str)}")

        if remain is not None and spend is not None:
            used = spend
            bucket = remain + spend
            pct = (used / bucket * 100.0) if bucket else 0.0
            bar_len = W - 32
            filled = int(round(pct / 100 * bar_len))
            filled = max(0, min(bar_len, filled))
            bar = "█" * filled + "░" * (bar_len - filled)
            lines.append(f"  Subscription used   {bar}  {pct:.1f}%")
            lines.append(f"  {c.dim('(against the USD value of credits in this billing period)')}")

    # Token usage panel
    lines.append("")
    lines.append(c.bold(c.cyan(f"▌ Token usage — {window_label}")))
    lines.append(sep)
    if usage.get("error"):
        lines.append(c.yellow("  " + usage["error"]))
    else:
        t = usage["totals"]
        total = t["input_tokens"] + t["output_tokens"] + t["cache_read"] + t["cache_write"]
        lines.append(f"  API calls      {c.bold(fmt_tokens(t['api_calls']))}     "
                     f"Total tokens   {c.bold(fmt_tokens(total))}")
        lines.append(f"  Input          {c.bold(fmt_tokens(t['input_tokens']))}")
        lines.append(f"  Output         {c.bold(fmt_tokens(t['output_tokens']))}")
        if t["reasoning_tokens"]:
            lines.append(f"  Reasoning      {c.dim(fmt_tokens(t['reasoning_tokens']))}")
        lines.append(f"  Cache read     {c.dim(fmt_tokens(t['cache_read']))}     "
                     f"Cache write    {c.dim(fmt_tokens(t['cache_write']))}")

        est = t["estimated_cost_usd"]
        act = t["actual_cost_usd"]
        cost_line = f"  Local est. cost  {c.yellow(fmt_usd(est))}"
        if act:
            cost_line += f"     actual: {c.yellow(fmt_usd(act))}"
        lines.append(cost_line)

        rows = usage["rows"]
        if rows:
            lines.append("")
            lines.append(c.bold(c.dim("  MODEL".ljust(34)) + c.dim("PROVIDER".ljust(14)) + c.dim("IN".rjust(9)) + c.dim("OUT".rjust(9)) + c.dim("COST".rjust(9))))
            for r in rows[:12]:
                model = (r["model"] or "unknown")
                prov = (r["billing_provider"] or "")[:13]
                inp = fmt_tokens(r["input_tokens"])
                out = fmt_tokens(r["output_tokens"])
                cost = fmt_usd(r["estimated_cost_usd"], 4)
                lines.append(
                    f"  {model[:33].ljust(34)}"
                    f"{prov.ljust(14)}"
                    f"{inp.rjust(9)}"
                    f"{out.rjust(9)}"
                    f"{c.yellow(cost.rjust(9))}"
                )
            if len(rows) > 12:
                lines.append(c.dim(f"  … and {len(rows)-12} more model/provider rows"))

    lines.append(sep)
    lines.append("")
    return "\n".join(lines)


def render_json(usage: dict, account: dict, window_label: str) -> str:
    out = {
        "window": window_label,
        "subscription": {k: v for k, v in account.items() if k != "error"},
        "subscription_error": account.get("error"),
        "usage_error": usage.get("error"),
        "totals": usage.get("totals", {}),
        "models": usage.get("rows", []),
    }
    return json.dumps(out, indent=2, default=str)


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nous-usage",
        description="Terminal usage dashboard for the Nous Portal subscription.",
    )
    p.add_argument("--days", type=int, default=30,
                   help="Look-back window in days (default: 30; 0 = all time)")
    p.add_argument("--today", action="store_true", help="Shorthand for today only")
    p.add_argument("--watch", action="store_true", help="Auto-refresh (default every 10s)")
    p.add_argument("--every", type=float, default=10.0, help="Refresh interval for --watch (seconds)")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON and exit")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                   help="Color output (default: auto)")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.today:
        start_of_today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        window_days = None
        window_cutoff = start_of_today.timestamp()
        window_key = "today"
    elif args.days == 0:
        window_days = None
        window_cutoff = None
        window_key = "all"
    else:
        window_days = args.days
        window_cutoff = None
        window_key = f"{args.days}d"

    window_label = {
        "today": "today",
        "all": "all time",
    }.get(window_key, f"last {args.days} days")

    color = sys.stdout.isatty() if args.color == "auto" else (args.color == "always")
    c = C(color)

    def snapshot() -> tuple[dict, dict]:
        usage = load_local_usage(window_days, cutoff_ts=window_cutoff)
        account = parse_subscription(fetch_portal_account())
        return usage, account

    if args.json:
        usage, account = snapshot()
        print(render_json(usage, account, window_label))
        return 0

    if not args.watch:
        usage, account = snapshot()
        print(render_dashboard(usage, account, window_label, c))
        return 0

    # Watch mode
    try:
        while True:
            clear_screen()
            usage, account = snapshot()
            print(render_dashboard(usage, account, window_label, c))
            print(c.dim(f"  Watching — refresh every {args.every}s   [q to quit]"))
            time.sleep(args.every)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
