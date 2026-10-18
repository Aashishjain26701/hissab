from __future__ import annotations

import hmac
import os
import csv
import io
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator

from flask import Flask, jsonify, render_template, request, session
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent
INSTANCE = ROOT / "instance"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
APP_ENV = os.environ.get("APP_ENV", "development")
if not DATABASE_URL:
    INSTANCE.mkdir(exist_ok=True)
DATABASE = Path(os.environ.get("DATABASE_PATH", str(INSTANCE / "hissab.db")))
if not DATABASE_URL:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)

if not APP_PASSWORD:
    raise RuntimeError(
        "Set APP_PASSWORD before starting AHARIVA Hissab. "
        "See README.md for local and deployment instructions."
    )
if APP_ENV == "production" and not os.environ.get("APP_SECRET_KEY"):
    raise RuntimeError("Set APP_SECRET_KEY to a persistent random value in production.")
if APP_ENV == "production" and not DATABASE_URL:
    raise RuntimeError("Set DATABASE_URL to persistent PostgreSQL before starting in production.")


class DatabaseConnection:
    def __init__(self, connection: Any, postgres: bool) -> None:
        self.connection = connection
        self.postgres = postgres

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        if self.postgres:
            statement = statement.replace("?", "%s")
        return self.connection.execute(statement, parameters)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=APP_SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=APP_ENV == "production",
)

DEFAULT_ACCOUNTS = (
    ("cash", "Business Cash", 1),
    ("bank", "Current Account", 1),
    ("marketplace", "Marketplace Clearing", 1),
    ("gst_reserve", "GST Reserve", 0),
    ("savings", "Savings Account", 1),
)


@contextmanager
def connect_db() -> Iterator[DatabaseConnection]:
    if DATABASE_URL:
        conn = psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
            sslmode="require" if APP_ENV == "production" else "prefer",
        )
        db = DatabaseConnection(conn, postgres=True)
    else:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        db = DatabaseConnection(conn, postgres=False)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    id_definition = "SERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with connect_db() as db:
        schema = [
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                opening_balance_paise INTEGER NOT NULL DEFAULT 0,
                cash_like INTEGER NOT NULL DEFAULT 1
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS products (
                id {id_definition},
                sku TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                stock_qty REAL NOT NULL DEFAULT 0,
                avg_cost_paise INTEGER NOT NULL DEFAULT 0,
                sell_price_paise INTEGER NOT NULL,
                gst_rate REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS transactions (
                id {id_definition},
                kind TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                partner TEXT NOT NULL DEFAULT '',
                product_id INTEGER REFERENCES products(id),
                quantity REAL NOT NULL DEFAULT 0,
                unit_price_paise INTEGER NOT NULL DEFAULT 0,
                net_paise INTEGER NOT NULL DEFAULT 0,
                gst_paise INTEGER NOT NULL DEFAULT 0,
                total_paise INTEGER NOT NULL DEFAULT 0,
                payment_account TEXT NOT NULL DEFAULT '',
                from_account TEXT NOT NULL DEFAULT '',
                to_account TEXT NOT NULL DEFAULT '',
                gst_credit_eligible INTEGER NOT NULL DEFAULT 0,
                restocked INTEGER NOT NULL DEFAULT 0,
                cogs_paise INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS ledger_movements (
                id {id_definition},
                transaction_id INTEGER NOT NULL REFERENCES transactions(id),
                account_key TEXT NOT NULL REFERENCES accounts(account_key),
                delta_paise INTEGER NOT NULL
            )
            """,
        ]
        for statement in schema:
            db.execute(statement)
        for key, name, cash_like in DEFAULT_ACCOUNTS:
            db.execute(
                "INSERT INTO accounts(account_key, name, cash_like) VALUES (?, ?, ?) ON CONFLICT (account_key) DO NOTHING",
                (key, name, cash_like),
            )


def money_to_paise(value: Any, field: str = "amount") -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a valid amount.")
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} cannot be negative.")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def quantity_value(value: Any) -> float:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Quantity must be a valid number.")
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")
    return float(quantity)


def tax_for(net_paise: int, rate: Any) -> int:
    try:
        percent = Decimal(str(rate or 0))
    except (InvalidOperation, ValueError):
        raise ValueError("GST rate must be a valid percentage.")
    if not percent.is_finite() or percent < 0 or percent > 100:
        raise ValueError("GST rate must be between 0 and 100.")
    return int((Decimal(net_paise) * percent / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def require_login(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not session.get("authenticated"):
            return jsonify(error="Please sign in to continue."), 401
        return function(*args, **kwargs)

    return wrapped


def add_movement(db: DatabaseConnection, transaction_id: int, account: str, delta: int) -> None:
    exists = db.execute("SELECT 1 FROM accounts WHERE account_key = ?", (account,)).fetchone()
    if not exists:
        raise ValueError("Choose a valid account.")
    db.execute(
        "INSERT INTO ledger_movements(transaction_id, account_key, delta_paise) VALUES (?, ?, ?)",
        (transaction_id, account, delta),
    )


def insert_transaction(
    db: DatabaseConnection,
    *,
    kind: str,
    transaction_date: str,
    description: str,
    category: str = "",
    channel: str = "",
    partner: str = "",
    product_id: int | None = None,
    quantity: float = 0,
    unit_price_paise: int = 0,
    net_paise: int = 0,
    gst_paise: int = 0,
    total_paise: int = 0,
    payment_account: str = "",
    from_account: str = "",
    to_account: str = "",
    gst_credit_eligible: bool = False,
    restocked: bool = False,
    cogs_paise: int = 0,
) -> int:
    statement = """INSERT INTO transactions(
        kind, transaction_date, description, category, channel, partner, product_id,
        quantity, unit_price_paise, net_paise, gst_paise, total_paise, payment_account,
        from_account, to_account, gst_credit_eligible, restocked, cogs_paise
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    cursor = db.execute(
        statement + (" RETURNING id" if db.postgres else ""),
        (
            kind,
            transaction_date,
            description,
            category,
            channel,
            partner,
            product_id,
            quantity,
            unit_price_paise,
            net_paise,
            gst_paise,
            total_paise,
            payment_account,
            from_account,
            to_account,
            int(gst_credit_eligible),
            int(restocked),
            cogs_paise,
        ),
    )
    if db.postgres:
        return int(cursor.fetchone()["id"])
    return int(cursor.lastrowid)


def validate_date(value: Any) -> str:
    raw = str(value or date.today().isoformat())
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ValueError("Date must use YYYY-MM-DD format.")


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/session")
def session_status() -> Any:
    return jsonify(authenticated=bool(session.get("authenticated")))


@app.post("/api/login")
def login() -> Any:
    password = str((request.get_json(silent=True) or {}).get("password", ""))
    if not hmac.compare_digest(password, APP_PASSWORD):
        return jsonify(error="Incorrect password."), 401
    session["authenticated"] = True
    return jsonify(ok=True)


@app.post("/api/logout")
@require_login
def logout() -> Any:
    session.clear()
    return jsonify(ok=True)


@app.get("/api/accounts")
@require_login
def accounts() -> Any:
    with connect_db() as db:
        rows = db.execute(
            """SELECT a.account_key AS key, a.name,
                      a.opening_balance_paise + COALESCE(SUM(m.delta_paise), 0) AS balance_paise
               FROM accounts a LEFT JOIN ledger_movements m ON m.account_key = a.account_key
               GROUP BY a.account_key
               ORDER BY CASE a.account_key WHEN 'cash' THEN 1 WHEN 'bank' THEN 2
                         WHEN 'marketplace' THEN 3 WHEN 'gst_reserve' THEN 4 ELSE 5 END"""
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/accounts/opening-balance")
@require_login
def set_opening_balance() -> Any:
    payload = request.get_json(silent=True) or {}
    key = str(payload.get("account_key", ""))
    try:
        amount = money_to_paise(payload.get("amount", 0), "Opening balance")
        with connect_db() as db:
            cursor = db.execute(
                "UPDATE accounts SET opening_balance_paise = ? WHERE account_key = ?", (amount, key)
            )
            if not cursor.rowcount:
                raise ValueError("Choose a valid account.")
        return jsonify(ok=True)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/products")
@require_login
def products() -> Any:
    with connect_db() as db:
        rows = db.execute("SELECT * FROM products ORDER BY lower(name)").fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/products")
@require_login
def create_product() -> Any:
    payload = request.get_json(silent=True) or {}
    sku = str(payload.get("sku", "")).strip()
    name = str(payload.get("name", "")).strip()
    if not sku or not name:
        return jsonify(error="SKU and product name are required."), 400
    try:
        price = money_to_paise(payload.get("sell_price"), "Selling price")
        if price == 0:
            raise ValueError("Selling price must be greater than zero.")
        opening_stock = float(Decimal(str(payload.get("opening_stock", 0))))
        unit_cost = money_to_paise(payload.get("unit_cost", 0), "Unit cost")
        rate = Decimal(str(payload.get("gst_rate", 0)))
        if not Decimal(str(opening_stock)).is_finite() or opening_stock < 0:
            raise ValueError("Opening stock cannot be negative.")
        if not rate.is_finite() or rate < 0 or rate > 100:
            raise ValueError("GST rate must be between 0 and 100.")
        with connect_db() as db:
            db.execute(
                """INSERT INTO products(sku, name, stock_qty, avg_cost_paise, sell_price_paise, gst_rate)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sku, name, opening_stock, unit_cost, price, float(rate)),
            )
        return jsonify(ok=True), 201
    except (sqlite3.IntegrityError, psycopg.IntegrityError):
        return jsonify(error="That SKU already exists."), 409
    except (ValueError, InvalidOperation) as exc:
        return jsonify(error=str(exc) or "Enter valid product values."), 400


@app.get("/api/dashboard")
@require_login
def dashboard() -> Any:
    start = date.today().replace(day=1).isoformat()
    with connect_db() as db:
        rows = db.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN kind = 'sale' THEN net_paise
                                   WHEN kind = 'sales_return' THEN -net_paise ELSE 0 END), 0) AS revenue,
                 COALESCE(SUM(CASE WHEN kind = 'sale' THEN gst_paise
                                   WHEN kind = 'sales_return' THEN -gst_paise ELSE 0 END), 0) AS output_gst,
                 COALESCE(SUM(CASE WHEN kind = 'purchase' AND gst_credit_eligible = 1 THEN gst_paise
                                   WHEN kind = 'expense' AND gst_credit_eligible = 1 THEN gst_paise
                                   ELSE 0 END), 0) AS input_gst,
                 COALESCE(SUM(CASE WHEN kind = 'sale' THEN cogs_paise
                                   WHEN kind = 'sales_return' THEN cogs_paise ELSE 0 END), 0) AS cogs,
                 COALESCE(SUM(CASE WHEN kind = 'expense' THEN net_paise ELSE 0 END), 0) AS expenses,
                 COALESCE(SUM(CASE WHEN kind = 'purchase' THEN net_paise ELSE 0 END), 0) AS purchases,
                 COALESCE(SUM(CASE WHEN kind = 'sale' AND transaction_date >= ? THEN net_paise
                                   WHEN kind = 'sales_return' AND transaction_date >= ? THEN -net_paise
                                   ELSE 0 END), 0) AS month_revenue,
                 COALESCE(SUM(CASE WHEN kind = 'expense' AND transaction_date >= ? THEN net_paise
                                   ELSE 0 END), 0) AS month_expenses
               FROM transactions""",
            (start, start, start),
        ).fetchone()
        inventory = db.execute(
            "SELECT COALESCE(SUM(stock_qty), 0) AS units, COALESCE(SUM(stock_qty * avg_cost_paise), 0) AS value FROM products"
        ).fetchone()
        account_rows = db.execute(
            """SELECT a.account_key, a.name, a.cash_like,
                      a.opening_balance_paise + COALESCE(SUM(m.delta_paise), 0) AS balance_paise
               FROM accounts a LEFT JOIN ledger_movements m ON m.account_key = a.account_key
               GROUP BY a.account_key
               ORDER BY CASE a.account_key WHEN 'cash' THEN 1 WHEN 'bank' THEN 2
                         WHEN 'marketplace' THEN 3 WHEN 'gst_reserve' THEN 4 ELSE 5 END"""
        ).fetchall()
        partners = db.execute(
            """SELECT partner,
                      SUM(CASE WHEN kind = 'partner_contribution' THEN total_paise
                               WHEN kind = 'partner_withdrawal' THEN -total_paise ELSE 0 END) AS balance_paise
               FROM transactions WHERE partner != '' GROUP BY partner ORDER BY partner"""
        ).fetchall()
        recent = db.execute(
            """SELECT t.*, p.name AS product_name FROM transactions t
               LEFT JOIN products p ON p.id = t.product_id ORDER BY t.id DESC LIMIT 8"""
        ).fetchall()
    revenue = int(rows["revenue"])
    cogs = int(rows["cogs"])
    expenses = int(rows["expenses"])
    output_gst = int(rows["output_gst"])
    input_gst = int(rows["input_gst"])
    return jsonify(
        metrics={
            "revenue_paise": revenue,
            "gross_profit_paise": revenue - cogs,
            "operating_profit_paise": revenue - cogs - expenses,
            "expenses_paise": expenses,
            "purchases_paise": int(rows["purchases"]),
            "month_revenue_paise": int(rows["month_revenue"]),
            "month_expenses_paise": int(rows["month_expenses"]),
            "inventory_units": float(inventory["units"]),
            "inventory_value_paise": int(inventory["value"]),
            "gst_output_paise": output_gst,
            "gst_input_paise": input_gst,
            "gst_estimate_paise": output_gst - input_gst,
        },
        accounts=[dict(row) for row in account_rows],
        partners=[dict(row) for row in partners],
        recent=[dict(row) for row in recent],
    )


@app.get("/api/transactions")
@require_login
def transaction_list() -> Any:
    with connect_db() as db:
        rows = db.execute(
            """SELECT t.*, p.name AS product_name FROM transactions t
               LEFT JOIN products p ON p.id = t.product_id ORDER BY t.id DESC LIMIT 300"""
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/transactions")
@require_login
def create_transaction() -> Any:
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind", ""))
    description = str(payload.get("description", "")).strip()
    if kind not in {"purchase", "sale", "sales_return", "expense", "partner_contribution", "partner_withdrawal", "transfer"}:
        return jsonify(error="Choose a valid transaction type."), 400
    if kind != "transfer" and not description:
        return jsonify(error="A description is required."), 400
    try:
        transaction_date = validate_date(payload.get("date"))
        with connect_db() as db:
            if kind in {"purchase", "sale", "sales_return"}:
                product_id = int(payload.get("product_id", 0))
                product_query = "SELECT * FROM products WHERE id = ?"
                if db.postgres:
                    product_query += " FOR UPDATE"
                product = db.execute(product_query, (product_id,)).fetchone()
                if product is None:
                    raise ValueError("Choose a valid product.")
                qty = quantity_value(payload.get("quantity"))
                if kind == "purchase":
                    unit = money_to_paise(payload.get("unit_price"), "Unit cost")
                    if unit == 0:
                        raise ValueError("Unit cost must be greater than zero.")
                    net = int((Decimal(str(qty)) * unit).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    gst = tax_for(net, payload.get("gst_rate", product["gst_rate"]))
                    total = net + gst
                    account = str(payload.get("payment_account", "bank"))
                    old_value = Decimal(str(product["stock_qty"])) * int(product["avg_cost_paise"])
                    incoming_value = Decimal(str(qty)) * (net if payload.get("gst_credit_eligible", True) else total)
                    new_qty = Decimal(str(product["stock_qty"])) + Decimal(str(qty))
                    avg_cost = int(((old_value + incoming_value) / new_qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    txid = insert_transaction(
                        db, kind=kind, transaction_date=transaction_date, description=description,
                        category="inventory", channel=str(payload.get("channel", "")), product_id=product_id,
                        quantity=qty, unit_price_paise=unit, net_paise=net, gst_paise=gst, total_paise=total,
                        payment_account=account, gst_credit_eligible=bool(payload.get("gst_credit_eligible", True)),
                    )
                    db.execute("UPDATE products SET stock_qty = ?, avg_cost_paise = ? WHERE id = ?", (float(new_qty), avg_cost, product_id))
                    add_movement(db, txid, account, -total)
                elif kind == "sale":
                    if float(product["stock_qty"]) + 1e-9 < qty:
                        raise ValueError(f"Not enough stock. Available: {product['stock_qty']:g}.")
                    unit = money_to_paise(payload.get("unit_price", product["sell_price_paise"] / 100), "Selling price")
                    if unit == 0:
                        raise ValueError("Selling price must be greater than zero.")
                    net = int((Decimal(str(qty)) * unit).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    gst = tax_for(net, product["gst_rate"])
                    total = net + gst
                    cogs = int((Decimal(str(qty)) * int(product["avg_cost_paise"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    account = str(payload.get("payment_account", "cash"))
                    txid = insert_transaction(
                        db, kind=kind, transaction_date=transaction_date, description=description,
                        channel=str(payload.get("channel", "Local")), product_id=product_id, quantity=qty,
                        unit_price_paise=unit, net_paise=net, gst_paise=gst, total_paise=total,
                        payment_account=account, cogs_paise=cogs,
                    )
                    db.execute("UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?", (qty, product_id))
                    add_movement(db, txid, account, total)
                else:
                    net = money_to_paise(payload.get("amount"), "Refund amount")
                    if net == 0:
                        raise ValueError("Refund amount must be greater than zero.")
                    gst = tax_for(net, product["gst_rate"])
                    total = net + gst
                    restocked = bool(payload.get("restocked", False))
                    cogs = 0
                    if restocked:
                        cogs = int((Decimal(str(qty)) * int(product["avg_cost_paise"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                        current_qty = Decimal(str(product["stock_qty"]))
                        new_qty = current_qty + Decimal(str(qty))
                        new_avg = int(((current_qty * int(product["avg_cost_paise"]) + cogs) / new_qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                        db.execute("UPDATE products SET stock_qty = ?, avg_cost_paise = ? WHERE id = ?", (float(new_qty), new_avg, product_id))
                    account = str(payload.get("payment_account", "bank"))
                    txid = insert_transaction(
                        db, kind=kind, transaction_date=transaction_date, description=description,
                        channel=str(payload.get("channel", "Online")), product_id=product_id, quantity=qty,
                        net_paise=net, gst_paise=gst, total_paise=total, payment_account=account,
                        restocked=restocked, cogs_paise=-cogs,
                    )
                    add_movement(db, txid, account, -total)
            elif kind == "expense":
                net = money_to_paise(payload.get("amount"), "Expense amount")
                if net == 0:
                    raise ValueError("Expense amount must be greater than zero.")
                gst = tax_for(net, payload.get("gst_rate", 0))
                total = net + gst
                account = str(payload.get("payment_account", "bank"))
                txid = insert_transaction(
                    db, kind=kind, transaction_date=transaction_date, description=description,
                    category=str(payload.get("category", "Other")), channel=str(payload.get("channel", "")),
                    net_paise=net, gst_paise=gst, total_paise=total, payment_account=account,
                    gst_credit_eligible=bool(payload.get("gst_credit_eligible", False)),
                )
                add_movement(db, txid, account, -total)
            elif kind in {"partner_contribution", "partner_withdrawal"}:
                amount = money_to_paise(payload.get("amount"), "Amount")
                if amount == 0:
                    raise ValueError("Amount must be greater than zero.")
                account = str(payload.get("payment_account", "bank"))
                partner = str(payload.get("partner", "")).strip()
                if not partner:
                    raise ValueError("Enter the partner name.")
                txid = insert_transaction(
                    db, kind=kind, transaction_date=transaction_date, description=description,
                    partner=partner, net_paise=amount, total_paise=amount, payment_account=account,
                )
                add_movement(db, txid, account, amount if kind == "partner_contribution" else -amount)
            else:
                amount = money_to_paise(payload.get("amount"), "Transfer amount")
                source = str(payload.get("from_account", ""))
                destination = str(payload.get("to_account", ""))
                if amount == 0 or source == destination:
                    raise ValueError("Enter a positive amount and choose two different accounts.")
                if not description:
                    description = f"Transfer: {source} to {destination}"
                txid = insert_transaction(
                    db, kind="transfer", transaction_date=transaction_date, description=description,
                    net_paise=amount, total_paise=amount, from_account=source, to_account=destination,
                )
                add_movement(db, txid, source, -amount)
                add_movement(db, txid, destination, amount)
        return jsonify(ok=True), 201
    except (sqlite3.IntegrityError, psycopg.IntegrityError):
        return jsonify(error="Transaction could not be saved because a referenced record is invalid."), 400
    except (ValueError, InvalidOperation) as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/export")
@require_login
def export_data() -> Any:
    from flask import Response

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Date", "Type", "Description", "Category", "Channel", "Partner", "Product", "Quantity",
         "Net amount (INR)", "GST (INR)", "Total (INR)", "Payment account"]
    )
    with connect_db() as db:
        rows = db.execute(
            """SELECT t.*, p.name AS product_name FROM transactions t
               LEFT JOIN products p ON p.id = t.product_id ORDER BY t.transaction_date, t.id"""
        )
        for row in rows:
            def safe_cell(value: Any) -> Any:
                if isinstance(value, str) and value.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
                    return "'" + value
                return value

            writer.writerow(
                [safe_cell(row["transaction_date"]), safe_cell(row["kind"]), safe_cell(row["description"]),
                 safe_cell(row["category"]), safe_cell(row["channel"]), safe_cell(row["partner"]),
                 safe_cell(row["product_name"] or ""), row["quantity"],
                 f'{row["net_paise"] / 100:.2f}', f'{row["gst_paise"] / 100:.2f}',
                 f'{row["total_paise"] / 100:.2f}', safe_cell(row["payment_account"])]
            )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ahariva-transactions.csv"},
    )


@app.errorhandler(500)
def internal_error(error: Exception) -> Any:
    app.logger.exception("Unhandled application error: %s", error)
    return jsonify(error="An unexpected error occurred. Check the server log for details."), 500


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
