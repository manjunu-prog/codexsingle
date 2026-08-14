"""Standalone professional paper-trading application."""

from __future__ import annotations

import streamlit as st

from api.fyers_login import FyersLogin
from api.option_chain import OptionChain
from config import FYERS
from paper_trading_ui import render_paper_trading


st.set_page_config(page_title="Option Terminal Paper Trading", layout="wide")


def secret_value(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


@st.cache_resource(show_spinner=False)
def get_client(credentials: dict):
    return FyersLogin(credentials=credentials).get_client()


@st.cache_data(ttl=8, show_spinner=False)
def load_quotes(_client, symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    response = _client.quotes(data={"symbols": ",".join(symbols)})
    if response.get("s") != "ok":
        return {}
    result = {}
    for item in response.get("d", []):
        symbol = item.get("n")
        value = item.get("v", {})
        try:
            ltp = float(value.get("lp") or value.get("ltp") or 0)
        except (TypeError, ValueError):
            ltp = 0.0
        if not symbol or ltp <= 0:
            continue
        def number(*keys):
            for key in keys:
                try:
                    if value.get(key) not in (None, ""):
                        return float(value[key])
                except (TypeError, ValueError):
                    pass
            return None
        result[symbol] = {
            "ltp": ltp,
            "bid": number("bid", "bidPrice", "bid_price"),
            "ask": number("ask", "askPrice", "ask_price"),
            "change": number("ch", "change", "netChg"),
            "change_pct": number("chp", "changePercent", "pctChg"),
        }
    return result


@st.cache_data(ttl=8, show_spinner=False)
def load_chain(_client, symbol: str, strikecount: int):
    return OptionChain(_client).fetch(symbol, strikecount=strikecount)


st.title("Option Terminal Paper Trading")
st.caption("Standalone app · live FYERS data · simulated orders only")

with st.sidebar:
    st.header("FYERS Login")
    credentials = {
        "FY_ID": st.text_input("Fyers ID", value=secret_value("FYERS_FY_ID", FYERS["FY_ID"])),
        "PIN": st.text_input("PIN", value=secret_value("FYERS_PIN", FYERS["PIN"]), type="password"),
        "TOTP_KEY": st.text_input("TOTP Key", value=secret_value("FYERS_TOTP_KEY", FYERS["TOTP_KEY"]), type="password"),
        "APP_ID": st.text_input("App ID", value=secret_value("FYERS_APP_ID", FYERS["APP_ID"])),
        "APP_SECRET": st.text_input("App Secret", value=secret_value("FYERS_APP_SECRET", FYERS["APP_SECRET"]), type="password"),
        "REDIRECT_URI": st.text_input("Redirect URI", value=secret_value("FYERS_REDIRECT_URI", FYERS["REDIRECT_URI"])),
    }

missing = [key for key, value in credentials.items() if not value and key != "REDIRECT_URI"]
if missing:
    st.info("Enter FYERS credentials in the sidebar or configure FYERS_* environment variables.")
    st.stop()

try:
    client = get_client(credentials)
except Exception as exc:
    st.error(f"FYERS login failed: {exc}")
    st.stop()

render_paper_trading(client, load_quotes, load_chain)
