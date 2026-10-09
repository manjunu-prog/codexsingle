"""Headless option-flow collector used by GitHub Actions."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# When launched as `python scripts/collect_option_flow.py`, Python starts with
# `scripts/` on sys.path. Add the repository root so `api.*` imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.fyers_login import FyersLogin
from api.option_chain import OptionChain

IST = ZoneInfo("Asia/Kolkata")


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def calculate_flow(chain):
    rows = {"CE": [], "PE": []}
    for item in chain:
        side = item.get("type")
        if side in rows:
            rows[side].append(item)

    result = {}
    for side in ("CE", "PE"):
        items = rows[side]
        oi = sum(number(item.get("oi")) for item in items)
        oi_change = sum(number(item.get("oi_change")) for item in items)
        previous_oi = oi - oi_change
        result[f"{side.lower()}_volume"] = sum(number(item.get("volume")) for item in items)
        result[f"{side.lower()}_oi"] = oi
        result[f"{side.lower()}_oi_change_abs"] = oi_change
        result[f"{side.lower()}_oi_change_pct"] = (oi_change / previous_oi * 100) if previous_oi else None
    return result


def main():
    symbol = os.getenv("OPTION_FLOW_SYMBOL", "NSE:NIFTY50-INDEX")
    credentials = {
        "FY_ID": os.environ["FYERS_FY_ID"],
        "PIN": os.environ["FYERS_PIN"],
        "TOTP_KEY": os.environ["FYERS_TOTP_KEY"],
        "APP_ID": os.environ["FYERS_APP_ID"],
        "APP_SECRET": os.environ["FYERS_APP_SECRET"],
        "REDIRECT_URI": os.getenv("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/redirect-uri/index.html"),
    }
    client = FyersLogin(credentials=credentials).get_client()
    chain = OptionChain(client).fetch(symbol, strikecount=int(os.getenv("OPTION_FLOW_STRIKECOUNT", "10")))
    if chain.empty:
        raise RuntimeError("FYERS returned an empty option chain")

    now = datetime.now(IST)
    date_key = now.date().isoformat()
    snapshot = calculate_flow(chain.to_dict(orient="records"))
    previous = fetch_previous(symbol, date_key)
    snapshot["pe_volume_change"] = snapshot["pe_volume"] - previous["pe_volume"] if previous else None
    snapshot["ce_volume_change"] = snapshot["ce_volume"] - previous["ce_volume"] if previous else None
    payload = {
        "snapshot_key": f"{symbol}|{date_key}|{now.strftime('%H:%M:%S')}",
        "snapshot_ts": now.isoformat(),
        "session_date": date_key,
        "snapshot_time": now.strftime("%H:%M:%S"),
        "symbol": symbol,
        **snapshot,
    }
    cfg = supabase_config()
    response = requests.post(
        f"{cfg['url']}/rest/v1/{cfg['table']}",
        params={"on_conflict": "snapshot_key"},
        headers={"apikey": cfg["key"], "Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
        json=[payload],
        timeout=20,
    )
    response.raise_for_status()
    print(f"Stored {payload['snapshot_key']}")


def supabase_config():
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY is required")
    return {"url": url, "key": key, "table": os.getenv("OPTION_FLOW_SUPABASE_TABLE", "option_flow_snapshots")}


def fetch_previous(symbol, session_date):
    cfg = supabase_config()
    response = requests.get(
        f"{cfg['url']}/rest/v1/{cfg['table']}",
        params={"select": "pe_volume,ce_volume", "symbol": f"eq.{symbol}", "session_date": f"eq.{session_date}", "order": "snapshot_ts.desc", "limit": "1"},
        headers={"apikey": cfg["key"], "Authorization": f"Bearer {cfg['key']}"},
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


if __name__ == "__main__":
    main()
