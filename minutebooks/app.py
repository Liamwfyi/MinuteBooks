from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as db_module


class ClientInputError(ValueError):
    pass


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    base_dir = Path(__file__).resolve().parents[1]
    secret_path = base_dir / "instance" / "secret_key"
    if os.environ.get("SECRET_KEY"):
        default_secret = os.environ["SECRET_KEY"]
    elif secret_path.exists():
        default_secret = secret_path.read_text().strip()
    else:
        default_secret = secrets.token_hex(32)
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(default_secret)
        
    app.config.from_mapping(
        SECRET_KEY=default_secret,
        DATABASE=str(base_dir / "instance" / "minutebooks.sqlite3"),
    )

    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db_module.init_app(app)

    with app.app_context():
        db_module.init_db()

    def log_activity(action: str, details: dict[str, Any] | None = None, user_id: int | None = None) -> None:
        try:
            db = db_module.get_db()
            db.execute(
                """
                INSERT INTO activity_log (user_id, action, timestamp, ip_address, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id if user_id is not None else session.get("user_id"),
                    action,
                    utcnow_iso(),
                    request.remote_addr,
                    json.dumps(details or {}),
                ),
            )
            db.commit()
        except Exception:
            pass

    def parse_date(value: str, field_name: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ClientInputError(f"{field_name} must be in YYYY-MM-DD format")

    def require_json(required_fields: list[str]) -> dict[str, Any]:
        payload = request.get_json(silent=True) or {}
        missing = [field for field in required_fields if field not in payload]
        if missing:
            raise ClientInputError(f"Missing required fields: {', '.join(missing)}")
        return payload

    def login_required(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return jsonify({"error": "Authentication required"}), 401
            return func(*args, **kwargs)

        return wrapper

    def fetch_row_or_404(table: str, row_id: int):
        queries = {
            "expenses": "SELECT * FROM expenses WHERE id = ? AND user_id = ?",
            "income": "SELECT * FROM income WHERE id = ? AND user_id = ?",
            "work_logs": "SELECT * FROM work_logs WHERE id = ? AND user_id = ?",
            "budgets": "SELECT * FROM budgets WHERE id = ? AND user_id = ?",
            "categories": "SELECT * FROM categories WHERE id = ? AND user_id = ?",
            "jobs": "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
        }
        query = queries.get(table)
        if query is None:
            raise ValueError("Unsupported table")
        db = db_module.get_db()
        row = db.execute(
            query,
            (row_id, session["user_id"]),
        ).fetchone()
        if row is None:
            return None
        return row

    def validate_fk(table: str, row_id: int | None) -> bool:
        if row_id is None:
            return True
        db = db_module.get_db()
        row = db.execute(f"SELECT id FROM {table} WHERE id = ? AND user_id = ?", (row_id, session["user_id"])).fetchone()
        return row is not None

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/register")
    def register():
        try:
            payload = require_json(["username", "password"])
            username = payload["username"].strip()
            password = payload["password"]
            if not username:
                raise ClientInputError("username cannot be empty")
            if len(password) < 6:
                raise ClientInputError("password must be at least 6 characters")

            db = db_module.get_db()
            now = utcnow_iso()
            cursor = db.execute(
                """
                INSERT INTO users (username, password_hash, preferred_currency, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (username, generate_password_hash(password), payload.get("currency", "USD"), now),
            )
            user_id = cursor.lastrowid
            db.execute(
                """
                INSERT INTO user_settings (user_id, currency)
                VALUES (?, ?)
                """,
                (user_id, payload.get("currency", "USD")),
            )
            default_categories = [
                "groceries",
                "entertainment",
                "transport",
                "dining",
                "utilities",
            ]
            for name in default_categories:
                db.execute(
                    "INSERT INTO categories (user_id, name, is_custom, created_at) VALUES (?, ?, 0, ?)",
                    (user_id, name, now),
                )
            db.commit()
            session.clear()
            session["user_id"] = user_id
            log_activity("register", {"username": username}, user_id=user_id)
            return jsonify({"id": user_id, "username": username}), 201
        except ClientInputError:
            return jsonify({"error": "Invalid registration payload"}), 400
        except sqlite3.IntegrityError:
            return jsonify({"error": "username already exists"}), 409

    @app.post("/login")
    def login():
        try:
            payload = require_json(["username", "password"])
            db = db_module.get_db()
            user = db.execute(
                "SELECT * FROM users WHERE username = ?",
                (payload["username"].strip(),),
            ).fetchone()
            if user is None or not check_password_hash(user["password_hash"], payload["password"]):
                return jsonify({"error": "Invalid username or password"}), 401

            now = utcnow_iso()
            db.execute(
                "UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
                (now, request.remote_addr, user["id"]),
            )
            db.commit()
            session.clear()
            session["user_id"] = user["id"]
            log_activity("login", {"username": user["username"]}, user_id=user["id"])
            return jsonify({"id": user["id"], "username": user["username"]})
        except ClientInputError:
            return jsonify({"error": "Invalid login payload"}), 400

    @app.post("/logout")
    @login_required
    def logout():
        user_id = session["user_id"]
        session.clear()
        log_activity("logout", user_id=user_id)
        return jsonify({"message": "Logged out"})

    @app.get("/expenses")
    @login_required
    def list_expenses():
        db = db_module.get_db()
        rows = db.execute(
            """
            SELECT e.*, c.name AS category_name
            FROM expenses e
            LEFT JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ?
            ORDER BY e.date_occurred DESC, e.id DESC
            """,
            (session["user_id"],),
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/expenses")
    @login_required
    def create_expense():
        try:
            payload = require_json(["amount", "category_id"])
            try:
                amount = float(payload["amount"])
            except (TypeError, ValueError):
                raise ClientInputError("amount must be a number")
            if amount < 0:
                raise ClientInputError("amount cannot be negative")

            category_id = payload.get("category_id")
            if not validate_fk("categories", category_id):
                return jsonify({"error": "Invalid category_id"}), 400

            occurred_str = payload.get("date_occurred", date.today().isoformat())
            occurred = parse_date(occurred_str, "date_occurred")
            pending = int(occurred > date.today())
            now = utcnow_iso()
            db = db_module.get_db()
            cursor = db.execute(
                """
                INSERT INTO expenses (user_id, amount, category_id, date_occurred, date_logged, is_pending, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["user_id"],
                    amount,
                    payload["category_id"],
                    occurred.isoformat(),
                    now,
                    pending,
                    payload.get("notes"),
                    now,
                ),
            )
            db.commit()
            log_activity("expense.create", {"expense_id": cursor.lastrowid})
            return jsonify({"id": cursor.lastrowid, "is_pending": bool(pending)}), 201
        except (ClientInputError, TypeError, ValueError):
            return jsonify({"error": "Invalid expense payload"}), 400

    @app.put("/expenses/<int:expense_id>")
    @login_required
    def update_expense(expense_id: int):
        row = fetch_row_or_404("expenses", expense_id)
        if row is None:
            return jsonify({"error": "Expense not found"}), 404
        payload = request.get_json(silent=True) or {}

        amount = float(payload.get("amount", row["amount"]))
        if amount < 0:
            return jsonify({"error": "amount cannot be negative"}), 400

        category_id = payload.get("category_id", row["category_id"])
        if not validate_fk("categories", category_id):
            return jsonify({"error": "Invalid category_id"}), 400

        occurred = parse_date(payload.get("date_occurred", row["date_occurred"]), "date_occurred")
        pending = int(occurred > date.today())

        db = db_module.get_db()
        db.execute(
            """
            UPDATE expenses
            SET amount = ?, category_id = ?, date_occurred = ?, is_pending = ?, notes = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                amount,
                category_id,
                occurred.isoformat(),
                pending,
                payload.get("notes", row["notes"]),
                expense_id,
                session["user_id"],
            ),
        )
        db.commit()
        log_activity("expense.update", {"expense_id": expense_id})
        return jsonify({"id": expense_id, "is_pending": bool(pending)})

    @app.delete("/expenses/<int:expense_id>")
    @login_required
    def delete_expense(expense_id: int):
        db = db_module.get_db()
        deleted = db.execute(
            "DELETE FROM expenses WHERE id = ? AND user_id = ?",
            (expense_id, session["user_id"]),
        ).rowcount
        db.commit()
        if not deleted:
            return jsonify({"error": "Expense not found"}), 404
        log_activity("expense.delete", {"expense_id": expense_id})
        return jsonify({"message": "Deleted"})

    def register_simple_crud(resource_name: str, table: str, amount_field: str = "amount"):
        list_route = f"/{resource_name}"
        item_route = f"/{resource_name}/<int:item_id>"
        singular_label = {
            "income": "Income",
            "work-logs": "Work log",
            "budgets": "Budget",
            "categories": "Category",
            "jobs": "Job",
        }.get(resource_name, "Item")

        @app.get(list_route, endpoint=f"list_{resource_name}")
        @login_required
        def list_items():
            db = db_module.get_db()
            list_queries = {
                "income": "SELECT * FROM income WHERE user_id = ? ORDER BY id DESC",
                "work_logs": "SELECT * FROM work_logs WHERE user_id = ? ORDER BY id DESC",
                "budgets": "SELECT * FROM budgets WHERE user_id = ? ORDER BY id DESC",
                "categories": "SELECT * FROM categories WHERE user_id = ? ORDER BY id DESC",
                "jobs": "SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC",
            }
            query = list_queries.get(table)
            if query is None:
                return jsonify({"error": "Unsupported table"}), 500
            rows = db.execute(
                query,
                (session["user_id"],),
            ).fetchall()
            return jsonify([dict(row) for row in rows])

        @app.post(list_route, endpoint=f"create_{resource_name}")
        @login_required
        def create_item():
            payload = request.get_json(silent=True) or {}
            db = db_module.get_db()
            now = utcnow_iso()

            if table == "income":
                amount = float(payload.get("amount", 0))
                if amount < 0:
                    return jsonify({"error": "amount cannot be negative"}), 400
                job_id = payload.get("job_id")
                if not validate_fk("jobs", job_id):
                    return jsonify({"error": "Invalid job_id"}), 400
                entry_date = parse_date(payload.get("date", date.today().isoformat()), "date")
                cursor = db.execute(
                    "INSERT INTO income (user_id, amount, job_id, date, logged_at) VALUES (?, ?, ?, ?, ?)",
                    (session["user_id"], amount, job_id, entry_date.isoformat(), now),
                )
            elif table == "work_logs":
                hours = float(payload.get("hours", 0))
                if hours < 0:
                    return jsonify({"error": "hours cannot be negative"}), 400
                job_id = payload.get("job_id")
                if not validate_fk("jobs", job_id):
                    return jsonify({"error": "Invalid job_id"}), 400
                entry_date = parse_date(payload.get("date", date.today().isoformat()), "date")
                cursor = db.execute(
                    "INSERT INTO work_logs (user_id, job_id, hours, date, logged_at) VALUES (?, ?, ?, ?, ?)",
                    (session["user_id"], job_id, hours, entry_date.isoformat(), now),
                )
            elif table == "budgets":
                amount = float(payload.get("amount", 0))
                if amount < 0:
                    return jsonify({"error": "amount cannot be negative"}), 400
                category_id = payload.get("category_id")
                if not validate_fk("categories", category_id):
                    return jsonify({"error": "Invalid category_id"}), 400
                period = payload.get("period", "month")
                cursor = db.execute(
                    "INSERT INTO budgets (user_id, category_id, amount, period) VALUES (?, ?, ?, ?)",
                    (session["user_id"], category_id, amount, period),
                )
            elif table == "categories":
                name = str(payload.get("name", "")).strip()
                if not name:
                    return jsonify({"error": "name cannot be empty"}), 400
                cursor = db.execute(
                    "INSERT INTO categories (user_id, name, is_custom, created_at) VALUES (?, ?, 1, ?)",
                    (session["user_id"], name, now),
                )
            elif table == "jobs":
                title = str(payload.get("title", "")).strip()
                if not title:
                    return jsonify({"error": "title cannot be empty"}), 400
                hourly_rate = float(payload.get("hourly_rate", 0))
                if hourly_rate < 0:
                    return jsonify({"error": "hourly_rate cannot be negative"}), 400
                cursor = db.execute(
                    "INSERT INTO jobs (user_id, title, hourly_rate) VALUES (?, ?, ?)",
                    (session["user_id"], title, hourly_rate),
                )
            else:
                return jsonify({"error": "Unsupported table"}), 500

            db.commit()
            log_activity(f"{table}.create", {"id": cursor.lastrowid})
            return jsonify({"id": cursor.lastrowid}), 201

        @app.put(item_route, endpoint=f"update_{resource_name}")
        @login_required
        def update_item(item_id: int):
            payload = request.get_json(silent=True) or {}
            row = fetch_row_or_404(table, item_id)
            if row is None:
                return jsonify({"error": f"{singular_label} not found"}), 404
            db = db_module.get_db()

            if table == "income":
                amount = float(payload.get("amount", row[amount_field]))
                if amount < 0:
                    return jsonify({"error": "amount cannot be negative"}), 400
                entry_date = parse_date(payload.get("date", row["date"]), "date")
                db.execute(
                    "UPDATE income SET amount = ?, job_id = ?, date = ? WHERE id = ? AND user_id = ?",
                    (amount, payload.get("job_id", row["job_id"]), entry_date.isoformat(), item_id, session["user_id"]),
                )
            elif table == "work_logs":
                hours = float(payload.get("hours", row["hours"]))
                if hours < 0:
                    return jsonify({"error": "hours cannot be negative"}), 400
                entry_date = parse_date(payload.get("date", row["date"]), "date")
                db.execute(
                    "UPDATE work_logs SET hours = ?, job_id = ?, date = ? WHERE id = ? AND user_id = ?",
                    (hours, payload.get("job_id", row["job_id"]), entry_date.isoformat(), item_id, session["user_id"]),
                )
            elif table == "budgets":
                amount = float(payload.get("amount", row[amount_field]))
                if amount < 0:
                    return jsonify({"error": "amount cannot be negative"}), 400
                db.execute(
                    "UPDATE budgets SET category_id = ?, amount = ?, period = ? WHERE id = ? AND user_id = ?",
                    (
                        payload.get("category_id", row["category_id"]),
                        amount,
                        payload.get("period", row["period"]),
                        item_id,
                        session["user_id"],
                    ),
                )
            elif table == "categories":
                name = str(payload.get("name", row["name"])).strip()
                if not name:
                    return jsonify({"error": "name cannot be empty"}), 400
                db.execute(
                    "UPDATE categories SET name = ? WHERE id = ? AND user_id = ?",
                    (name, item_id, session["user_id"]),
                )

            db.commit()
            log_activity(f"{table}.update", {"id": item_id})
            return jsonify({"id": item_id})

        @app.delete(item_route, endpoint=f"delete_{resource_name}")
        @login_required
        def delete_item(item_id: int):
            db = db_module.get_db()
            delete_queries = {
                "income": "DELETE FROM income WHERE id = ? AND user_id = ?",
                "work_logs": "DELETE FROM work_logs WHERE id = ? AND user_id = ?",
                "budgets": "DELETE FROM budgets WHERE id = ? AND user_id = ?",
                "categories": "DELETE FROM categories WHERE id = ? AND user_id = ?",
            }
            query = delete_queries.get(table)
            if query is None:
                return jsonify({"error": "Unsupported table"}), 500
            deleted = db.execute(
                query,
                (item_id, session["user_id"]),
            ).rowcount
            db.commit()
            if not deleted:
                return jsonify({"error": f"{singular_label} not found"}), 404
            log_activity(f"{table}.delete", {"id": item_id})
            return jsonify({"message": "Deleted"})

    register_simple_crud("income", "income")
    register_simple_crud("work-logs", "work_logs", amount_field="hours")
    register_simple_crud("budgets", "budgets")
    register_simple_crud("categories", "categories")

    @app.get("/settings")
    @login_required
    def get_settings():
        db = db_module.get_db()
        row = db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (session["user_id"],),
        ).fetchone()
        if row is None:
            return jsonify({"error": "Settings not found"}), 404
        return jsonify(dict(row))

    @app.put("/settings")
    @login_required
    def update_settings():
        payload = request.get_json(silent=True) or {}
        allowed_fields = {
            "dark_mode",
            "color_primary",
            "color_background",
            "color_accent",
            "color_text",
            "currency",
        }
        if not any(key in payload for key in allowed_fields):
            return jsonify({"error": "No valid settings provided"}), 400

        db = db_module.get_db()
        existing = db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (session["user_id"],),
        ).fetchone()
        if existing is None:
            return jsonify({"error": "Settings not found"}), 404

        db.execute(
            """
            UPDATE user_settings
            SET dark_mode = ?, color_primary = ?, color_background = ?, color_accent = ?, color_text = ?, currency = ?
            WHERE user_id = ?
            """,
            (
                int(payload.get("dark_mode", existing["dark_mode"])),
                payload.get("color_primary", existing["color_primary"]),
                payload.get("color_background", existing["color_background"]),
                payload.get("color_accent", existing["color_accent"]),
                payload.get("color_text", existing["color_text"]),
                payload.get("currency", existing["currency"]),
                session["user_id"],
            ),
        )
        db.commit()
        log_activity("settings.update", {"updated_fields": [key for key in payload if key in allowed_fields]})
        return jsonify({"message": "Settings updated"})

    @app.get("/analytics")
    @login_required
    def analytics():
        db = db_module.get_db()
        user_id = session["user_id"]
        totals = db.execute(
            """
            SELECT
                COALESCE((SELECT SUM(amount) FROM income WHERE user_id = ?), 0) AS total_income,
                COALESCE((SELECT SUM(amount) FROM expenses WHERE user_id = ?), 0) AS total_expenses,
                COALESCE((SELECT SUM(hours) FROM work_logs WHERE user_id = ?), 0) AS total_hours
            """,
            (user_id, user_id, user_id),
        ).fetchone()

        by_category = db.execute(
            """
            SELECT c.name AS category, COALESCE(SUM(e.amount), 0) AS total
            FROM categories c
            LEFT JOIN expenses e ON e.category_id = c.id AND e.user_id = c.user_id
            WHERE c.user_id = ?
            GROUP BY c.id, c.name
            ORDER BY total DESC, c.name ASC
            """,
            (user_id,),
        ).fetchall()

        return jsonify(
            {
                "totals": dict(totals),
                "balance": totals["total_income"] - totals["total_expenses"],
                "spending_by_category": [dict(row) for row in by_category],
            }
        )

    return app
