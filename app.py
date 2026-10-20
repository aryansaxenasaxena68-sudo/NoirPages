
import os
import hashlib
import secrets
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal

import psycopg
import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    render_template,
    abort,
    send_file,
    request,
    jsonify,
    redirect,
    url_for,
)

load_dotenv()

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_BOOKS_DIR = (BASE_DIR / "private_books").resolve()
CASHFREE_API_VERSION = "2025-01-01"


def get_database_url():
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def cashfree_config():
    mode = os.getenv("CASHFREE_ENV", "sandbox").strip().lower()

    if mode not in ("sandbox", "production"):
        raise RuntimeError("CASHFREE_ENV must be sandbox or production")

    app_id = os.getenv("CASHFREE_APP_ID")
    secret = os.getenv("CASHFREE_SECRET_KEY")

    if not app_id or not secret:
        raise RuntimeError("Cashfree credentials are not configured")

    api_base = (
        "https://sandbox.cashfree.com/pg"
        if mode == "sandbox"
        else "https://api.cashfree.com/pg"
    )

    return api_base, app_id, secret


def cashfree_headers(app_id, secret):
    return {
        "x-client-id": app_id,
        "x-client-secret": secret,
        "x-api-version": CASHFREE_API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


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
        return render_template("index.html", books=get_books())
    except Exception:
        app.logger.exception("Failed to load books")
        return "Unable to load books. Check Render logs.", 500


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

        return {"status": "connected", "database": database}

    except Exception:
        app.logger.exception("Database test failed")
        return jsonify(
            status="error",
            message="Database connection failed",
        ), 500


@app.route("/api/create-order", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}

    try:
        book_id = int(data.get("book_id", 0))
    except (TypeError, ValueError):
        return jsonify(error="Invalid book selection"), 400

    if book_id <= 0:
        return jsonify(error="Invalid book selection"), 400

    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()

    if not name or len(name) > 100:
        return jsonify(error="Please enter your name"), 400

    if len(email) > 254 or not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+", email
    ):
        return jsonify(error="Please enter a valid email address"), 400

    try:
        api_base, app_id, secret = cashfree_config()

        # Read product price from the database, never from the browser.
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, price_paise
                    FROM books
                    WHERE id = %s AND is_active = TRUE
                    """,
                    (book_id,),
                )
                book = cur.fetchone()

                if not book:
                    return jsonify(error="This eBook is unavailable"), 404

                title, price_paise = book
                amount = Decimal(price_paise) / Decimal(100)

                # FIX: Reuse the customer if their email already exists.
                # The email column has a unique constraint in the database.
                cur.execute(
                    """
                    INSERT INTO customers (name, email)
                    VALUES (%s, %s)
                    ON CONFLICT (email)
                    DO UPDATE SET name = EXCLUDED.name
                    RETURNING id
                    """,
                    (name, email),
                )
                customer_id = cur.fetchone()[0]

                provider_order_id = "np_" + secrets.token_hex(12)

                cur.execute(
                    """
                    INSERT INTO orders (
                        customer_id,
                        book_id,
                        amount_paise,
                        currency,
                        payment_status,
                        payment_provider,
                        provider_order_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        customer_id,
                        book_id,
                        price_paise,
                        "INR",
                        "pending",
                        "cashfree",
                        provider_order_id,
                    ),
                )
                local_order_id = cur.fetchone()[0]

        base_url = os.getenv(
            "BASE_URL", "https://noirpages.onrender.com"
        ).rstrip("/")

        payload = {
            "order_id": provider_order_id,
            "order_amount": float(amount),
            "order_currency": "INR",
            "customer_details": {
                "customer_id": f"customer_{customer_id}",
                "customer_name": name,
                "customer_email": email,
                "customer_phone": "9999999999",
            },
            "order_meta": {
                "return_url": (
                    f"{base_url}/payment/return"
                    f"?order_id={provider_order_id}"
                )
            },
            "order_note": f"eBook: {title}"[:250],
        }

        response = requests.post(
            f"{api_base}/orders",
            headers=cashfree_headers(app_id, secret),
            json=payload,
            timeout=20,
        )

        if not response.ok:
            app.logger.error(
                "Cashfree order creation failed: HTTP %s; %s",
                response.status_code,
                response.text[:1000],
            )
            return jsonify(
                error="Payment setup failed. Please try again later."
            ), 502

        result = response.json()
        session_id = result.get("payment_session_id")

        if not session_id:
            app.logger.error(
                "Cashfree response had no payment session ID"
            )
            return jsonify(
                error="Payment session could not be created"
            ), 502

        return jsonify(
            order_id=provider_order_id,
            payment_session_id=session_id,
            mode=os.getenv("CASHFREE_ENV", "sandbox").strip().lower(),
        )

    except requests.RequestException:
        app.logger.exception("Cashfree order request failed")
        return jsonify(
            error="Payment service is temporarily unavailable. Please try again."
        ), 502

    except Exception:
        app.logger.exception("Could not create payment order")
        return jsonify(
            error="Unable to start checkout. Please try again."
        ), 500


def verify_cashfree_order(provider_order_id):
    api_base, app_id, secret = cashfree_config()

    response = requests.get(
        f"{api_base}/orders/{provider_order_id}",
        headers=cashfree_headers(app_id, secret),
        timeout=20,
    )

    if not response.ok:
        app.logger.error(
            "Cashfree verification failed: HTTP %s; %s",
            response.status_code,
            response.text[:1000],
        )
        return None

    return response.json()


def create_download_token(order_id):
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(
        raw_token.encode("utf-8")
    ).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO download_tokens (
                    order_id, token_hash, expires_at
                )
                SELECT o.id, %s, %s
                FROM orders o
                JOIN books b ON b.id = o.book_id
                WHERE o.id = %s
                  AND LOWER(o.payment_status) = 'paid'
                  AND b.is_active = TRUE
                  AND b.file_path IS NOT NULL
                  AND b.file_path <> ''
                RETURNING id
                """,
                (token_hash, expires_at, order_id),
            )

            if cur.fetchone() is None:
                raise ValueError(
                    "No eligible paid order or book file"
                )

    return raw_token


@app.route("/payment/return")
def payment_return():
    provider_order_id = request.args.get("order_id", "").strip()

    if not provider_order_id or len(provider_order_id) > 100:
        return "Invalid payment order.", 400

    try:
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, amount_paise, currency
                    FROM orders
                    WHERE provider_order_id = %s
                      AND payment_provider = 'cashfree'
                    """,
                    (provider_order_id,),
                )
                local_order = cur.fetchone()

        if not local_order:
            return "Order not found.", 404

        local_order_id, expected_paise, currency = local_order

        provider_order = verify_cashfree_order(provider_order_id)

        if not provider_order:
            return (
                "Payment status could not be verified. "
                "Please contact support before trying to pay again."
            ), 503

        status = str(
            provider_order.get("order_status", "")
        ).upper()

        try:
            actual_amount = Decimal(
                str(provider_order.get("order_amount"))
            )
            expected_amount = Decimal(expected_paise) / Decimal(100)
        except Exception:
            app.logger.error(
                "Invalid amount in Cashfree order response"
            )
            return "Payment verification failed.", 502

        actual_currency = str(
            provider_order.get("order_currency", "")
        ).upper()

        if (
            status == "PAID"
            and actual_amount == expected_amount
            and actual_currency == str(currency).strip().upper()
        ):
            with psycopg.connect(
                get_database_url(), connect_timeout=10
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE orders
                        SET payment_status = 'paid',
                            paid_at = COALESCE(paid_at, NOW())
                        WHERE id = %s
                          AND provider_order_id = %s
                        """,
                        (local_order_id, provider_order_id),
                    )

            token = create_download_token(local_order_id)
            return redirect(url_for("download_page", token=token))

        if status in ("ACTIVE", "CREATED"):
            return (
                "Payment is still pending. Refresh this page after "
                "completing payment, or contact support if money was deducted."
            ), 202

        return (
            "Payment was not confirmed. No download has been unlocked. "
            "If money was deducted, please contact support."
        ), 402

    except requests.RequestException:
        app.logger.exception("Cashfree verification request failed")
        return "Payment verification is temporarily unavailable.", 503

    except Exception:
        app.logger.exception("Payment return handling failed")
        return "Could not confirm payment. Please contact support.", 500


@app.route("/payment/success/<token>")
def download_page(token):
    if not token or len(token) > 100:
        abort(404)

    return render_template("download.html", token=token)


@app.route("/download/<token>")
def download_book(token):
    if not token or len(token) > 100:
        abort(404)

    token_hash = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()

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

    try:
        requested_path = (BASE_DIR / file_path).resolve()
        requested_path.relative_to(PRIVATE_BOOKS_DIR)
    except (TypeError, ValueError, OSError):
        app.logger.error("Rejected unsafe book file path")
        abort(404)

    if not requested_path.is_file():
        app.logger.error(
            "Book PDF file not found for book: %s", title
        )
        return "The eBook file is not currently available.", 503

    return send_file(
        requested_path,
        as_attachment=True,
        download_name=requested_path.name,
        mimetype="application/pdf",
        conditional=True,
    )


if __name__ == "__main__":
    app.run(debug=False)
