# nous-usage

Track your [Nous Portal](https://portal.nousresearch.com) subscription spend and your local
[Hermes](https://hermes-agent.nousresearch.com) model usage. Two zero-dependency tools, both
pure Python 3 stdlib:

- **`nous_usage.py`** — a live terminal dashboard (subscription state, per-model token usage, time
  windows, `--watch` auto-refresh, `--json` output).
- **`nous_usage_server.py`** — a self-hosted HTTP JSON API you can point a web dashboard, status
  page, or e-Paper display at (`GET /usage`).

---

## What it reads

| Source | Path | What |
|--------|------|------|
| **Nous Portal account API** | `https://portal.nousresearch.com/api/oauth/account` | subscription plan, spend this period, cap/credits, period end |
| **Local Hermes session store** | `~/.hermes/state.db` (SQLite, read-only) | per-model token counts and estimated cost |

The Portal bearer token is read from `~/.hermes/auth.json` at runtime — **no keys are embedded in
the code or committed to the repo.** The local DB is opened **read-only**; nothing is ever written.

> Prefer a different home? Set `HERMES_HOME` and both tools pick it up automatically.

---

## Terminal dashboard

```bash
python3 nous_usage.py                  # last 30 days
python3 nous_usage.py --days 7         # last 7 days
python3 nous_usage.py --today          # today only
python3 nous_usage.py --watch          # auto-refresh every 10s (q / Ctrl-C to quit)
python3 nous_usage.py --watch --every 5
python3 nous_usage.py --json           # machine-readable JSON, exit
```

---

## HTTP JSON server

```bash
python3 nous_usage_server.py --port 8765
```

| Endpoint | Returns |
|----------|---------|
| `GET /usage` | JSON snapshot: `spend_usd`, `cap_usd`, `pct`, `days_left`, `on_pace_usd`, `period_end`, `total_tokens`, `today_tokens`, `models[]` |
| `GET /` | small HTML status page for a quick browser check |

Example:

```bash
curl -s localhost:8765/usage | python3 -m json.tool
```

The Portal fetch is cached for 5 minutes so polling clients don't hammer the API.

> **Security:** the server binds to `127.0.0.1` (localhost) by default, so it never exposes
> your personal usage over the network. To reach it from another machine you must
> explicitly pass `--host 0.0.0.0` — only do this on a trusted network, and be aware
> it serves *your* subscription spend and model usage to whoever can reach the port.
> This tool is for your own tracking; it is not a service to host for others.

---

## Requirements

- Python 3.9+
- A Nous Portal account (any paid tier)
- A Hermes install with a logged-in Nous provider (`~/.hermes/auth.json`)

No third-party dependencies.

## License

MIT
