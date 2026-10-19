import os
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from flask import Flask, render_template, abort, send_file

load_dotenv()

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_BOOKS_DIR = (BASE_DIR / "private_books").resolve()


def get_database_url():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured in .env")

    return database_url


def get_books():
    query = """
        SELECT id, title, category, description,
               price_paise, cover_path
        FROM books
        WHERE is_active = TRUE
        ORDER BY id
    """

    with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "title": row[1],
            "category": row[2],
            "description": row[3],
            "price": row[4] / 100,
            "icon": "✦",
            "cover": row[5] or "cover-ai",
        }
        for row in rows
    ]


@app.route("/")
def home():
    try:
        books = get_books()
        return render_template("index.html", books=books)
    except Exception:
        app.logger.exception("Failed to load books")
        return "Unable to load books. Check the Flask terminal.", 500


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/db-test")
def db_test():
    try:
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                database = cur.fetchone()[0]

        return {
            "status": "connected",
            "database": database,
            "message": "Neon database connection successful",
        }
    except Exception:
        app.logger.exception("Database connection test failed")
        return {
            "status": "error",
            "message": "Database connection failed. Check the terminal logs.",
        }, 500


def create_download_token(order_id):
    """
    Call this function ONLY after verifying a successful payment
    directly with the payment provider or a verified webhook.

    It refuses to create a token for an unpaid order.
    The raw token is returned once; only its hash is stored.
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    query = """
        INSERT INTO download_tokens (order_id, token_hash, expires_at)
        SELECT o.id, %s, %s
        FROM orders o
        JOIN books b ON b.id = o.book_id
        WHERE o.id = %s
          AND LOWER(o.payment_status) = 'paid'
          AND b.is_active = TRUE
          AND b.file_path IS NOT NULL
          AND b.file_path <> ''
        RETURNING id
    """

    with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (token_hash, expires_at, order_id))
            result = cur.fetchone()

            if result is None:
                raise ValueError(
                    "No eligible paid order found, or the book has no file path."
                )

    return raw_token, expires_at


@app.route("/download/<token>")
def download_book(token):
    # Accept only tokens in the expected URL-safe format and length.
    if not token or len(token) > 100:
        abort(404)

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    query = """
        SELECT b.title, b.file_path
        FROM download_tokens dt
        JOIN orders o ON o.id = dt.order_id
        JOIN books b ON b.id = o.book_id
        WHERE dt.token_hash = %s
          AND dt.expires_at > NOW()
          AND LOWER(o.payment_status) = 'paid'
          AND b.is_active = TRUE
        LIMIT 1
    """

    try:
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (token_hash,))
                row = cur.fetchone()
    except Exception:
        app.logger.exception("Download database check failed")
        return "Download temporarily unavailable.", 503

    if row is None:
        abort(404)

    title, file_path = row

    # Resolve the database path and prevent directory traversal.
    try:
        requested_path = (BASE_DIR / file_path).resolve()
        requested_path.relative_to(PRIVATE_BOOKS_DIR)
    except (TypeError, ValueError, OSError):
        app.logger.error("Rejected unsafe book file path")
        abort(404)

    if not requested_path.is_file():
        app.logger.error("Book PDF file not found for book: %s", title)
        abort(404)

    return send_file(
        requested_path,
        as_attachment=True,
        download_name=requested_path.name,
        mimetype="application/pdf",
        conditional=True,
    )


if __name__ == "__main__":
    app.run(debug=True)