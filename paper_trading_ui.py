"""Streamlit UI for the isolated 12-portfolio paper-trading workspace."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from api.option_chain import OptionChain
from config import INDEX_CONFIG
from paper_trading import PaperTradingStore


def _money(value) -> str:
    return f"₹{float(value or 0):,.2f}"


def _quote_symbols(store: PaperTradingStore, portfolio_id: str, selected_symbol: str = "") -> list[str]:
    symbols = {selected_symbol} if selected_symbol else set()
    positions = store.positions(portfolio_id)
    if not positions.empty:
        symbols.update(positions["symbol"].tolist())
    return sorted(symbols)


def _portfolio_selector(store: PaperTradingStore) -> tuple[pd.DataFrame, str]:
    portfolios = store.portfolios()
    labels = {row.id: f"{row.instrument} · {row.name}" for row in portfolios.itertuples()}
    selected = st.selectbox("Paper portfolio", list(labels), format_func=labels.get, key="paper_portfolio")
    return portfolios, selected


def render_paper_trading(client, quote_loader, chain_loader=None) -> None:
    st.title("Professional Paper Trading")
    st.caption("Live FYERS market data · simulated orders only · twelve isolated analysis portfolios")
    store = PaperTradingStore()
    portfolios, portfolio_id = _portfolio_selector(store)
    portfolio = store.portfolio(portfolio_id)

    instrument = portfolio["instrument"]
    config = INDEX_CONFIG[instrument]
    try:
        chain = chain_loader(client, config["spot"], 25) if chain_loader else OptionChain(client).fetch(config["spot"], strikecount=25)
    except Exception as exc:
        st.error(f"Unable to load live {instrument} option chain: {exc}")
        chain = pd.DataFrame()

    # Resolve the current symbol universe before processing pending orders.
    position_symbols = _quote_symbols(store, portfolio_id)
    chain_symbols = chain["symbol"].dropna().tolist() if not chain.empty and "symbol" in chain else []
    live_quotes = quote_loader(client, sorted(set(position_symbols + chain_symbols))) if (position_symbols or chain_symbols) else {}
    filled = store.process_pending(live_quotes)
    if filled:
        st.toast(f"Filled {filled} pending paper order(s)")
    store.snapshot(portfolio_id, live_quotes)
    stats = store.stats(portfolio_id, live_quotes)

    metric_cols = st.columns(6)
    metric_cols[0].metric("Equity", _money(stats["equity"]))
    metric_cols[1].metric("Cash", _money(stats["cash"]))
    metric_cols[2].metric("Unrealized P&L", _money(stats["unrealized_pnl"]))
    metric_cols[3].metric("Available Margin", _money(stats["available_margin"]))
    metric_cols[4].metric("Open Positions", len(stats["positions"]))
    metric_cols[5].metric("Pending Orders", int((store.orders(portfolio_id)["status"] == "PENDING").sum()))

    with st.expander("Portfolio settings", expanded=False):
        setting_cols = st.columns(4)
        expiry_value = setting_cols[0].text_input("Assigned expiry label", value=portfolio["expiry"] or "", key=f"expiry:{portfolio_id}")
        capital = setting_cols[1].number_input("Starting capital", min_value=1000.0, value=float(portfolio["initial_capital"]), step=10000.0, key=f"capital:{portfolio_id}")
        max_loss = setting_cols[2].number_input("Max daily loss", min_value=0.0, value=float(portfolio["max_daily_loss"]), step=1000.0, key=f"loss:{portfolio_id}")
        max_order = setting_cols[3].number_input("Max order value", min_value=1000.0, value=float(portfolio["max_order_value"]), step=10000.0, key=f"order_limit:{portfolio_id}")
        if st.button("Save portfolio settings", key=f"save_settings:{portfolio_id}"):
            store.update_portfolio(portfolio_id, expiry=expiry_value, initial_capital=capital, max_daily_loss=max_loss, max_order_value=max_order)
            st.success("Portfolio settings saved")

    st.subheader(f"{instrument} order ticket")
    if chain.empty:
        st.warning("No option contracts are available from FYERS at the moment.")
        chain_view = pd.DataFrame()
    else:
        chain_view = chain.copy()
        if "expiry" not in chain_view:
            chain_view["expiry"] = "Current"
        chain_view["expiry"] = chain_view["expiry"].replace("", "Current").fillna("Current").astype(str)
        expiry_options = sorted(chain_view["expiry"].unique().tolist())
        default_expiry = portfolio["expiry"] if portfolio["expiry"] in expiry_options else expiry_options[0]
        expiry = st.selectbox("Expiry", expiry_options, index=expiry_options.index(default_expiry), key=f"ticket_expiry:{portfolio_id}")
        chain_view = chain_view[chain_view["expiry"] == expiry].copy()

    if chain_view.empty:
        return

    chain_view["contract"] = chain_view.apply(
        lambda row: f"{row['strike']:g} {row['type']} · {row['symbol']}", axis=1
    )
    contract_options = chain_view["contract"].tolist()
    selected_contract = st.selectbox("Contract", contract_options, key=f"ticket_contract:{portfolio_id}")
    contract = chain_view[chain_view["contract"] == selected_contract].iloc[0]
    symbol = contract["symbol"]
    quote = live_quotes.get(symbol) or quote_loader(client, [symbol]).get(symbol, {})
    ltp = float(quote.get("ltp") or contract.get("ltp") or 0)
    qcols = st.columns(5)
    qcols[0].metric("LTP", _money(ltp))
    qcols[1].metric("Bid", _money(quote.get("bid")))
    qcols[2].metric("Ask", _money(quote.get("ask")))
    qcols[3].metric("OI", f"{float(contract.get('oi', 0) or 0):,.0f}")
    qcols[4].metric("IV", f"{float(contract.get('iv', 0) or 0):,.2f}")

    with st.form(f"order_form:{portfolio_id}", clear_on_submit=False):
        order_cols = st.columns(6)
        side = order_cols[0].selectbox("Side", ["BUY", "SELL"], key=f"side:{portfolio_id}")
        order_type = order_cols[1].selectbox("Order type", ["MARKET", "LIMIT", "SL", "SL-M"], key=f"type:{portfolio_id}")
        quantity = order_cols[2].number_input("Quantity", min_value=1, value=1, step=1, key=f"qty:{portfolio_id}")
        product = order_cols[3].selectbox("Product", ["MIS", "NRML"], key=f"product:{portfolio_id}")
        price = order_cols[4].number_input("Limit / trigger", min_value=0.0, value=float(ltp), step=0.05, format="%.2f", key=f"price:{portfolio_id}")
        tag = order_cols[5].text_input("Trade tag", value="manual", key=f"tag:{portfolio_id}")
        submitted = st.form_submit_button("Place paper order", type="primary")
    if submitted:
        try:
            result = store.submit_order(
                portfolio_id,
                {
                    "instrument": instrument,
                    "expiry": str(contract.get("expiry", "")),
                    "symbol": symbol,
                    "option_type": contract["type"],
                    "strike": float(contract["strike"]),
                    "side": side,
                    "quantity": int(quantity),
                    "order_type": order_type,
                    "limit_price": float(price) if order_type == "LIMIT" else None,
                    "stop_price": float(price) if order_type in {"SL", "SL-M"} else None,
                    "product": product,
                    "tag": tag,
                },
                quote,
            )
            st.success(f"{result['status']}: {side} {quantity} {symbol}")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    tabs = st.tabs(["Positions", "Orders", "All-portfolios overview", "Reports"])
    with tabs[0]:
        positions = store.positions(portfolio_id)
        if positions.empty:
            st.info("No open positions in this portfolio.")
        else:
            display = positions.copy()
            display["LTP"] = display["symbol"].map(lambda item: (live_quotes.get(item) or {}).get("ltp"))
            display["Unrealized P&L"] = (display["quantity"] * (display["LTP"] - display["average_price"])).round(2)
            st.dataframe(display, use_container_width=True, hide_index=True)
    with tabs[1]:
        orders = store.orders(portfolio_id)
        if not orders.empty:
            pending = orders[orders["status"].isin(["PENDING", "PARTIAL"])]
            if not pending.empty:
                cancel_id = st.selectbox("Pending order to cancel", pending["id"].tolist(), key=f"cancel:{portfolio_id}")
                if st.button("Cancel selected order", key=f"cancel_button:{portfolio_id}"):
                    store.cancel_order(cancel_id)
                    st.rerun()
            st.dataframe(orders, use_container_width=True, hide_index=True)
        else:
            st.info("No orders yet.")
    with tabs[2]:
        overview = []
        for row in portfolios.itertuples():
            row_quotes = quote_loader(client, [item["symbol"] for item in store.stats(row.id).get("positions", [])]) if False else live_quotes
            row_stats = store.stats(row.id, row_quotes)
            overview.append({"Portfolio": row.name, "Instrument": row.instrument, "Expiry": row.expiry or "Unassigned", "Equity": row_stats["equity"], "Cash": row_stats["cash"], "Unrealized P&L": row_stats["unrealized_pnl"], "Open positions": len(row_stats["positions"])})
        st.dataframe(pd.DataFrame(overview), use_container_width=True, hide_index=True)
    with tabs[3]:
        st.caption("Exports include separate sheets for portfolios, orders, fills, and positions.")
        export_cols = st.columns(2)
        csv_data = store.export(portfolio_id, "csv")
        export_cols[0].download_button("Download portfolio CSV", csv_data, file_name=f"{portfolio['name']}_paper_trades.csv", mime="text/csv")
        try:
            xlsx_data = store.export(portfolio_id, "xlsx")
            export_cols[1].download_button("Download portfolio Excel", xlsx_data, file_name=f"{portfolio['name']}_paper_trades.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as exc:
            export_cols[1].warning(f"Excel export unavailable: {exc}")
        st.download_button("Download all 12 portfolios CSV", store.export(None, "csv"), file_name="all_paper_trades.csv", mime="text/csv")
        with st.expander("Reset this paper portfolio", expanded=False):
            st.warning("This removes its paper orders, fills, positions, and snapshots. It does not affect FYERS.")
            confirm = st.checkbox("I understand this cannot be undone", key=f"reset_confirm:{portfolio_id}")
            if st.button("Reset selected portfolio", disabled=not confirm, key=f"reset:{portfolio_id}"):
                store.reset_portfolio(portfolio_id)
                st.success("Paper portfolio reset")
                st.rerun()

