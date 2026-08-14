"""Persistent paper-trading ledger for Option Terminal Pro.

This module never calls a broker order endpoint.  FYERS is used only for live
quotes; fills, cash, positions, risk, and reporting are maintained locally.
"""

from __future__ import annotations

import io
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


DEFAULT_PORTFOLIOS = [
    ("NIFTY", f"NIFTY-SLOT-{slot}") for slot in range(1, 5)
] + [
    ("BANKNIFTY", f"BANKNIFTY-SLOT-{slot}") for slot in range(1, 5)
] + [
    ("SENSEX", f"SENSEX-SLOT-{slot}") for slot in range(1, 5)
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperTradingStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or Path(__file__).parent / "data" / "paper_trading.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS portfolios (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    instrument TEXT NOT NULL,
                    expiry TEXT DEFAULT '',
                    initial_capital REAL NOT NULL DEFAULT 100000,
                    cash REAL NOT NULL DEFAULT 100000,
                    max_daily_loss REAL NOT NULL DEFAULT 5000,
                    max_order_value REAL NOT NULL DEFAULT 250000,
                    margin_rate REAL NOT NULL DEFAULT 0.20,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    expiry TEXT DEFAULT '',
                    symbol TEXT NOT NULL,
                    option_type TEXT DEFAULT '',
                    strike REAL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    order_type TEXT NOT NULL,
                    limit_price REAL,
                    stop_price REAL,
                    average_fill REAL,
                    status TEXT NOT NULL,
                    product TEXT NOT NULL DEFAULT 'MIS',
                    tag TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    FOREIGN KEY(portfolio_id) REFERENCES portfolios(id)
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL,
                    filled_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    value REAL NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );
                CREATE TABLE IF NOT EXISTS positions (
                    portfolio_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    expiry TEXT DEFAULT '',
                    option_type TEXT DEFAULT '',
                    strike REAL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    average_price REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(portfolio_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    margin_used REAL NOT NULL
                );
                """
            )
            existing = conn.execute("SELECT COUNT(*) AS count FROM portfolios").fetchone()["count"]
            if existing == 0:
                for instrument, name in DEFAULT_PORTFOLIOS:
                    capital = 100000.0
                    conn.execute(
                        """INSERT INTO portfolios
                        (id,name,instrument,initial_capital,cash,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), name, instrument, capital, capital, utc_now(), utc_now()),
                    )

    def portfolios(self) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query("SELECT * FROM portfolios ORDER BY instrument, name", conn)

    def portfolio(self, portfolio_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
            if not row:
                raise KeyError("Portfolio not found")
            return dict(row)

    def update_portfolio(self, portfolio_id: str, **values) -> None:
        allowed = {"name", "expiry", "initial_capital", "max_daily_loss", "max_order_value", "margin_rate", "active"}
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._connect() as conn:
            conn.execute(f"UPDATE portfolios SET {assignments} WHERE id=?", (*values.values(), portfolio_id))

    def positions(self, portfolio_id: str) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query(
                "SELECT * FROM positions WHERE portfolio_id=? AND quantity != 0 ORDER BY symbol",
                conn,
                params=(portfolio_id,),
            )

    def orders(self, portfolio_id: str | None = None, limit: int = 200) -> pd.DataFrame:
        query = "SELECT * FROM orders"
        params: tuple = ()
        if portfolio_id:
            query += " WHERE portfolio_id=?"
            params = (portfolio_id,)
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=(*params, limit))

    def fills(self, portfolio_id: str | None = None, limit: int = 500) -> pd.DataFrame:
        query = "SELECT * FROM fills"
        params: tuple = ()
        if portfolio_id:
            query += " WHERE portfolio_id=?"
            params = (portfolio_id,)
        query += " ORDER BY filled_at DESC LIMIT ?"
        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=(*params, limit))

    def _today_realized(self, conn: sqlite3.Connection, portfolio_id: str) -> float:
        row = conn.execute(
            """SELECT COALESCE(SUM(p.realized_pnl),0) AS pnl FROM positions p
               WHERE p.portfolio_id=?""",
            (portfolio_id,),
        ).fetchone()
        return float(row["pnl"] or 0)

    def _portfolio_stats(self, conn, portfolio: dict, quotes: dict[str, dict]) -> dict:
        rows = conn.execute("SELECT * FROM positions WHERE portfolio_id=? AND quantity != 0", (portfolio["id"],)).fetchall()
        market_value = unrealized = margin_used = 0.0
        position_rows = []
        for row in rows:
            quote = quotes.get(row["symbol"], {})
            ltp = float(quote.get("ltp") or row["average_price"])
            quantity = int(row["quantity"])
            value = quantity * ltp
            upnl = quantity * (ltp - float(row["average_price"]))
            market_value += value
            unrealized += upnl
            if quantity < 0:
                margin_used += abs(value) * float(portfolio["margin_rate"])
            position_rows.append({"symbol": row["symbol"], "ltp": ltp, "market_value": value, "unrealized_pnl": upnl})
        cash = float(portfolio["cash"])
        equity = cash + market_value
        return {
            "cash": cash,
            "market_value": market_value,
            "equity": equity,
            "unrealized_pnl": unrealized,
            "margin_used": margin_used,
            "available_margin": equity - margin_used,
            "positions": position_rows,
        }

    def stats(self, portfolio_id: str, quotes: dict[str, dict] | None = None) -> dict:
        quotes = quotes or {}
        with self._connect() as conn:
            return self._portfolio_stats(conn, self.portfolio(portfolio_id), quotes)

    def submit_order(self, portfolio_id: str, order: dict, quote: dict | None = None) -> dict:
        quote = quote or {}
        side = str(order["side"]).upper()
        order_type = str(order["order_type"]).upper()
        quantity = int(order["quantity"])
        if side not in {"BUY", "SELL"} or quantity <= 0:
            raise ValueError("Side must be BUY/SELL and quantity must be positive")
        if order_type not in {"MARKET", "LIMIT", "SL", "SL-M"}:
            raise ValueError("Unsupported order type")
        ltp = float(quote.get("ltp") or 0)
        execution_price = float(quote.get("ask") or ltp) if side == "BUY" else float(quote.get("bid") or ltp)
        if execution_price <= 0 and order_type == "MARKET":
            raise ValueError("A live LTP is required for a market paper order")
        reference = float(order.get("limit_price") or order.get("stop_price") or execution_price or 0)
        if reference <= 0:
            raise ValueError("Price is required for this order")
        order_id = str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            portfolio = dict(conn.execute("SELECT * FROM portfolios WHERE id=?", (portfolio_id,)).fetchone())
            stats = self._portfolio_stats(conn, portfolio, {order["symbol"]: quote})
            value = reference * quantity
            if value > float(portfolio["max_order_value"]):
                raise ValueError(f"Order value {value:,.2f} exceeds portfolio limit")
            if stats["available_margin"] <= 0 and side == "SELL":
                raise ValueError("Available margin is exhausted")
            status = "PENDING"
            if order_type == "MARKET":
                status = "FILLED"
            conn.execute(
                """INSERT INTO orders
                (id,portfolio_id,created_at,updated_at,instrument,expiry,symbol,option_type,strike,side,
                 quantity,order_type,limit_price,stop_price,average_fill,status,product,tag,reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order_id, portfolio_id, now, now, order.get("instrument", ""), order.get("expiry", ""), order["symbol"],
                    order.get("option_type", ""), order.get("strike"), side, quantity, order_type,
                    order.get("limit_price"), order.get("stop_price"), execution_price if status == "FILLED" else None,
                    status, order.get("product", "MIS"), order.get("tag", ""), "",
                ),
            )
            if status == "FILLED":
                self._fill_locked(conn, order_id, execution_price, quantity)
            return dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())

    def _fill_locked(self, conn: sqlite3.Connection, order_id: str, price: float, quantity: int | None = None) -> None:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or row["status"] == "CANCELLED":
            return
        fill_qty = quantity or (int(row["quantity"]) - int(row["filled_quantity"]))
        if fill_qty <= 0:
            return
        portfolio = dict(conn.execute("SELECT * FROM portfolios WHERE id=?", (row["portfolio_id"],)).fetchone())
        signed_qty = fill_qty if row["side"] == "BUY" else -fill_qty
        existing = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id=? AND symbol=?", (row["portfolio_id"], row["symbol"])
        ).fetchone()
        old_qty = int(existing["quantity"]) if existing else 0
        old_avg = float(existing["average_price"]) if existing else 0.0
        realized = float(existing["realized_pnl"]) if existing else 0.0
        new_qty = old_qty + signed_qty
        if old_qty and (old_qty > 0) != (signed_qty > 0):
            closed = min(abs(old_qty), abs(signed_qty))
            realized += closed * (price - old_avg) * (1 if old_qty > 0 else -1)
        if new_qty == 0:
            new_avg = 0.0
        elif old_qty == 0 or (old_qty > 0) == (signed_qty > 0):
            new_avg = ((abs(old_qty) * old_avg) + (abs(signed_qty) * price)) / abs(new_qty)
        else:
            new_avg = price if abs(signed_qty) > abs(old_qty) else old_avg
        cash_delta = -signed_qty * price
        conn.execute("UPDATE portfolios SET cash=cash+?, updated_at=? WHERE id=?", (cash_delta, utc_now(), row["portfolio_id"]))
        conn.execute(
            """INSERT INTO positions (portfolio_id,symbol,instrument,expiry,option_type,strike,quantity,average_price,realized_pnl,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(portfolio_id,symbol) DO UPDATE SET quantity=excluded.quantity,
               average_price=excluded.average_price,realized_pnl=excluded.realized_pnl,updated_at=excluded.updated_at""",
            (row["portfolio_id"], row["symbol"], row["instrument"], row["expiry"], row["option_type"], row["strike"], new_qty, new_avg, realized, utc_now()),
        )
        fill_value = fill_qty * price
        conn.execute(
            "INSERT INTO fills (id,order_id,portfolio_id,filled_at,symbol,side,quantity,price,value) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), order_id, row["portfolio_id"], utc_now(), row["symbol"], row["side"], fill_qty, price, fill_value),
        )
        filled_total = int(row["filled_quantity"]) + fill_qty
        status = "FILLED" if filled_total >= int(row["quantity"]) else "PARTIAL"
        conn.execute(
            "UPDATE orders SET filled_quantity=?,average_fill=?,status=?,updated_at=? WHERE id=?",
            (filled_total, price, status, utc_now(), order_id),
        )

    def process_pending(self, quotes: dict[str, dict]) -> int:
        filled = 0
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM orders WHERE status IN ('PENDING','PARTIAL')").fetchall()
            for row in rows:
                quote = quotes.get(row["symbol"], {})
                ltp = float(quote.get("ltp") or 0)
                if ltp <= 0:
                    continue
                limit_price = row["limit_price"]
                stop_price = row["stop_price"]
                triggered = row["order_type"] == "MARKET"
                if row["order_type"] == "LIMIT":
                    triggered = ltp <= limit_price if row["side"] == "BUY" else ltp >= limit_price
                elif row["order_type"] in {"SL", "SL-M"}:
                    triggered = ltp >= stop_price if row["side"] == "BUY" else ltp <= stop_price
                if triggered:
                    price = float(limit_price or stop_price or ltp) if row["order_type"] == "LIMIT" else ltp
                    self._fill_locked(conn, row["id"], price)
                    filled += 1
        return filled

    def cancel_order(self, order_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE orders SET status='CANCELLED',updated_at=? WHERE id=? AND status IN ('PENDING','PARTIAL')", (utc_now(), order_id))

    def snapshot(self, portfolio_id: str, quotes: dict[str, dict]) -> None:
        with self._connect() as conn:
            portfolio = dict(conn.execute("SELECT * FROM portfolios WHERE id=?", (portfolio_id,)).fetchone())
            stats = self._portfolio_stats(conn, portfolio, quotes)
            conn.execute(
                "INSERT INTO equity_snapshots VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), portfolio_id, utc_now(), stats["equity"], stats["cash"], stats["market_value"], stats["unrealized_pnl"], stats["margin_used"]),
            )

    def reset_portfolio(self, portfolio_id: str) -> None:
        with self._connect() as conn:
            portfolio = dict(conn.execute("SELECT * FROM portfolios WHERE id=?", (portfolio_id,)).fetchone())
            conn.execute("DELETE FROM fills WHERE portfolio_id=?", (portfolio_id,))
            conn.execute("DELETE FROM orders WHERE portfolio_id=?", (portfolio_id,))
            conn.execute("DELETE FROM positions WHERE portfolio_id=?", (portfolio_id,))
            conn.execute("DELETE FROM equity_snapshots WHERE portfolio_id=?", (portfolio_id,))
            conn.execute("UPDATE portfolios SET cash=initial_capital,updated_at=? WHERE id=?", (utc_now(), portfolio_id))

    def export(self, portfolio_id: str | None = None, fmt: str = "csv") -> bytes:
        frames = {
            "portfolios": self.portfolios() if not portfolio_id else pd.DataFrame([self.portfolio(portfolio_id)]),
            "orders": self.orders(portfolio_id),
            "fills": self.fills(portfolio_id),
            "positions": self.positions(portfolio_id) if portfolio_id else self._all_positions(),
        }
        if fmt == "xlsx":
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                for name, frame in frames.items():
                    frame.to_excel(writer, index=False, sheet_name=name[:31])
            return output.getvalue()
        chunks = []
        for name, frame in frames.items():
            chunks.append(f"# {name}\n{frame.to_csv(index=False)}")
        return "\n".join(chunks).encode("utf-8")

    def _all_positions(self) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query("SELECT * FROM positions ORDER BY portfolio_id,symbol", conn)
