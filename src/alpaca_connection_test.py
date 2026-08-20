import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path("results_alpaca_test")
OUT.mkdir(exist_ok=True)

key = os.getenv("ALPACA_API_KEY", "").strip()
secret = os.getenv("ALPACA_SECRET_KEY", "").strip()

summary = {
    "credentials_present": bool(key and secret),
    "market_data_ok": False,
    "feed": "iex",
    "symbol": "SPY",
}

if not key or not secret:
    summary["error"] = "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY GitHub secret"
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    sys.exit(1)

headers = {
    "APCA-API-KEY-ID": key,
    "APCA-API-SECRET-KEY": secret,
    "Accept": "application/json",
}

# IEX is used deliberately because it is available to Alpaca's basic/free market-data plan.
params = urllib.parse.urlencode({"symbols": "SPY", "feed": "iex"})
url = f"https://data.alpaca.markets/v2/stocks/snapshots?{params}"
req = urllib.request.Request(url, headers=headers)

try:
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    snap = payload.get("SPY") or payload.get("snapshots", {}).get("SPY")
    if not snap:
        raise RuntimeError(f"No SPY snapshot returned; top-level keys={list(payload)[:10]}")
    summary["market_data_ok"] = True
    summary["snapshot_fields"] = sorted(snap.keys())
    # Never write credentials to output.
    (OUT / "spy_snapshot.json").write_text(json.dumps(snap, indent=2))
except Exception as exc:
    summary["error"] = f"{type(exc).__name__}: {exc}"

(OUT / "summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
sys.exit(0 if summary["market_data_ok"] else 1)
