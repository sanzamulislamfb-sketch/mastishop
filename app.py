"""
Nagad/bKash SMS Forwarder - Cloud Receiver Server + Order Verification System
Now using Firebase Firestore for storage (persists across restarts/redeploys).

Local run:
    pip install -r requirements.txt
    Place your Firebase service account key as firebase-key.json in this folder
    python app.py

Then set the phone app's API URL to:
    https://<YOUR-RENDER-URL>/api/sms-webhook

ENV VARS to set on Render:
    API_TOKEN                - existing SMS webhook token (unchanged)
    ADMIN_PASSWORD            - password to log into /admin
    SECRET_KEY                - any random string, used to sign the admin session cookie
    BKASH_NUMBER              - your bKash personal number that customers send money to
    NAGAD_NUMBER              - your Nagad personal number that customers send money to
    FIREBASE_CREDENTIALS_JSON - the FULL content of your Firebase service account
                                JSON file, pasted as one value (see setup notes)

WHY FIRESTORE:
    Render's free/starter web services use an ephemeral filesystem - any
    local file (like a SQLite .db file) is wiped whenever the service
    restarts, redeploys, or spins back up after idling. That's why orders
    and messages were disappearing. Firestore is a separate managed cloud
    database, so the data now survives all of that.
"""

import os
import re
import json
import uuid
import hashlib
import requests
from functools import wraps
from datetime import datetime

from flask import Flask, request, jsonify, render_template, session, redirect, url_for

import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_TOKEN = os.environ.get("API_TOKEN", "")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

PAYMENT_NUMBERS = {
    "bkash": os.environ.get("BKASH_NUMBER", "01700000000"),
    "nagad": os.environ.get("NAGAD_NUMBER", "01800000000"),
}

PRODUCTS = {
    "basic": {"name": "Masti Ghor", "price": 99.00},
}

# ---------------------------------------------------------------------------
# Firebase init
# ---------------------------------------------------------------------------

def init_firebase():
    if firebase_admin._apps:
        return

    creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if creds_json:
        cred_dict = json.loads(creds_json)
        cred = credentials.Certificate(cred_dict)
    else:
        # Local development fallback: put your downloaded service account
        # key file next to this script, named firebase-key.json
        cred = credentials.Certificate("firebase-key.json")

    firebase_admin.initialize_app(cred)


init_firebase()
db = firestore.client()

MESSAGES_COLLECTION = "messages"
ORDERS_COLLECTION = "orders"
SETTINGS_COLLECTION = "settings"
SETTINGS_DOC_ID = "payment_numbers"


def get_payment_numbers():
    """Firestore-এর settings doc থেকে bKash/Nagad নাম্বার পড়ে।
    কিছু সেভ করা না থাকলে ENV VAR-এর ডিফল্ট নাম্বার ব্যবহার হবে।"""
    doc = db.collection(SETTINGS_COLLECTION).document(SETTINGS_DOC_ID).get()
    if doc.exists:
        data = doc.to_dict()
        return {
            "bkash": data.get("bkash") or PAYMENT_NUMBERS["bkash"],
            "nagad": data.get("nagad") or PAYMENT_NUMBERS["nagad"],
        }
    return dict(PAYMENT_NUMBERS)


def set_payment_numbers(bkash_number, nagad_number):
    db.collection(SETTINGS_COLLECTION).document(SETTINGS_DOC_ID).set({
        "bkash": bkash_number,
        "nagad": nagad_number,
    }, merge=True)


PIXEL_SETTINGS_DOC_ID = "meta_pixel"


def get_pixel_config():
    """Firestore থেকে Facebook Pixel ID + CAPI access token পড়ে।"""
    doc = db.collection(SETTINGS_COLLECTION).document(PIXEL_SETTINGS_DOC_ID).get()
    if doc.exists:
        data = doc.to_dict()
        return {
            "pixel_id": data.get("pixel_id") or "",
            "pixel_token": data.get("pixel_token") or "",
        }
    return {"pixel_id": "", "pixel_token": ""}


def set_pixel_config(pixel_id, pixel_token):
    db.collection(SETTINGS_COLLECTION).document(PIXEL_SETTINGS_DOC_ID).set({
        "pixel_id": pixel_id,
        "pixel_token": pixel_token,
    }, merge=True)


def send_purchase_event(order_data):
    """Order approve হওয়ার পর Meta Conversions API-তে সার্ভার-সাইড
    Purchase event পাঠায়, যাতে ad boost/campaign-এ conversion হিসেবে
    কাউন্ট হয়। Pixel সেটাপ করা না থাকলে চুপচাপ কিছু করে না।"""
    config = get_pixel_config()
    pixel_id = config["pixel_id"]
    pixel_token = config["pixel_token"]

    if not pixel_id or not pixel_token:
        return  # Pixel connect করা নেই, তাই কিছু পাঠাবে না

    try:
        amount = float(order_data.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0

    user_data = {}
    if order_data.get("customer_ip"):
        user_data["client_ip_address"] = order_data["customer_ip"]
    if order_data.get("user_agent"):
        user_data["client_user_agent"] = order_data["user_agent"]
    if order_data.get("customer_fbp"):
        user_data["fbp"] = order_data["customer_fbp"]
    if order_data.get("customer_fbc"):
        user_data["fbc"] = order_data["customer_fbc"]
    if order_data.get("customer_external_id"):
        hashed_ext_id = hashlib.sha256(
            order_data["customer_external_id"].strip().encode("utf-8")
        ).hexdigest()
        user_data["external_id"] = [hashed_ext_id]
    if order_data.get("payer_number"):
        phone_digits = re.sub(r"\D", "", str(order_data["payer_number"]))
        if phone_digits.startswith("0"):
            phone_digits = "88" + phone_digits
        hashed_phone = hashlib.sha256(phone_digits.encode("utf-8")).hexdigest()
        user_data["ph"] = [hashed_phone]

    event_payload = {
        "data": [{
            "event_name": "Purchase",
            "event_id": order_data.get("order_id"),
            "event_time": int(datetime.now().timestamp()),
            "action_source": "website",
            "event_source_url": "https://mastighor.shop/order",
            "user_data": user_data,
            "custom_data": {
                "currency": "BDT",
                "value": amount,
                "content_name": order_data.get("product_name"),
                "order_id": order_data.get("order_id"),
            },
        }]
    }

    test_event_code = os.environ.get("META_TEST_EVENT_CODE", "").strip()
    if test_event_code:
        event_payload["test_event_code"] = test_event_code

    url = f"https://graph.facebook.com/v21.0/{pixel_id}/events"
    try:
        resp = requests.post(
            url,
            params={"access_token": pixel_token},
            json=event_payload,
            timeout=8,
        )
        print(f"[Meta CAPI] Response {resp.status_code}: {resp.text}")
        if resp.status_code != 200:
            print(f"[Meta CAPI] Purchase event failed for order {order_data.get('order_id')}")
        else:
            print(f"[Meta CAPI] Purchase event sent for order {order_data.get('order_id')}")
    except requests.RequestException as e:
        print(f"[Meta CAPI] Purchase event error: {e}")

@app.route("/api/capi/event", methods=["POST"])
def capi_event():
    """Client-triggered server-side CAPI events (PageView / ViewContent / InitiateCheckout).
    Fired alongside the browser Pixel so Meta can deduplicate using the shared event_id,
    boosting Event Match Quality without collecting phone/email."""
    config = get_pixel_config()
    pixel_id = config["pixel_id"]
    pixel_token = config["pixel_token"]

    if not pixel_id or not pixel_token:
        return jsonify({"status": "ok"})  # Pixel not configured — skip silently

    data = request.get_json(silent=True) or {}
    event_name = data.get("event_name", "")

    if event_name not in ("PageView", "ViewContent", "InitiateCheckout"):
        return jsonify({"status": "error", "message": "Invalid event name"}), 400

    event_id = data.get("event_id") or ("capi_" + uuid.uuid4().hex[:12])
    event_source_url = data.get("event_source_url") or "https://mastighor.shop/"

    customer_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    user_agent = request.headers.get("User-Agent", "")

    user_data = {
        "client_ip_address": customer_ip,
        "client_user_agent": user_agent,
    }
    if data.get("fbp"):
        user_data["fbp"] = data["fbp"]
    if data.get("fbc"):
        user_data["fbc"] = data["fbc"]
    if data.get("external_id"):
        hashed_ext = hashlib.sha256(
            str(data["external_id"]).strip().encode("utf-8")
        ).hexdigest()
        user_data["external_id"] = [hashed_ext]

    event_entry = {
        "event_name": event_name,
        "event_id": event_id,
        "event_time": int(datetime.now().timestamp()),
        "action_source": "website",
        "event_source_url": event_source_url,
        "user_data": user_data,
    }

    custom_data = {}
    if data.get("content_name"):
        custom_data["content_name"] = data["content_name"]
    if data.get("content_ids"):
        custom_data["content_ids"] = data["content_ids"]
    if data.get("content_type"):
        custom_data["content_type"] = data["content_type"]
    if data.get("value") is not None:
        try:
            custom_data["value"] = float(data["value"])
            custom_data["currency"] = data.get("currency", "BDT")
        except (TypeError, ValueError):
            pass
    if custom_data:
        event_entry["custom_data"] = custom_data

    event_payload = {"data": [event_entry]}
    test_event_code = os.environ.get("META_TEST_EVENT_CODE", "").strip()
    if test_event_code:
        event_payload["test_event_code"] = test_event_code

    url = f"https://graph.facebook.com/v21.0/{pixel_id}/events"
    try:
        resp = requests.post(
            url,
            params={"access_token": pixel_token},
            json=event_payload,
            timeout=8,
        )
        print(f"[Meta CAPI] {event_name} Response {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as e:
        print(f"[Meta CAPI] {event_name} error: {e}")

    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# SMS parsing helpers (unchanged)
# ---------------------------------------------------------------------------

def extract_amount(message: str):
    match = re.search(r"Tk\.?\s?([\d,]+\.?\d*)", message, re.IGNORECASE)
    if match:
        return match.group(1).replace(",", "")
    return None


def extract_trx_id(message: str):
    match = re.search(r"T(?:xn|rx)ID:?\s?([A-Za-z0-9]+)", message, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_payer_number(message: str):
    match = re.search(r"(?:Sender:|from)\s?(01\d{9})", message, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def normalize_amount(value):
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Order matching helpers (Firestore versions)
# ---------------------------------------------------------------------------

def find_matching_message(txn_id, amount):
    """Find a stored SMS message with a matching txn_id + amount that
    hasn't already been used to approve a different order.

    IMPORTANT: once a txn_id has been used to approve ANY order, it can
    never be used to approve another order again - even if a duplicate
    SMS message with the same txn_id exists in the messages collection.
    This prevents the same transaction ID from being reused to approve
    multiple orders.
    """
    if not txn_id:
        return None

    target_amount = normalize_amount(amount)
    txn_upper = txn_id.upper()

    # Block reuse: if this txn_id has already approved an order, stop here.
    already_approved = list(
        db.collection(ORDERS_COLLECTION)
        .where("submitted_txn_id_upper", "==", txn_upper)
        .where("status", "==", "approved")
        .limit(1)
        .stream()
    )
    if already_approved:
        return None

    query = db.collection(MESSAGES_COLLECTION).where("trx_id_upper", "==", txn_upper)
    docs = list(query.stream())
    # newest first
    docs.sort(key=lambda d: d.to_dict().get("received_at", ""), reverse=True)

    for doc in docs:
        data = doc.to_dict()
        if normalize_amount(data.get("amount")) != target_amount:
            continue

        already_used = list(
            db.collection(ORDERS_COLLECTION)
            .where("matched_message_id", "==", doc.id)
            .limit(1)
            .stream()
        )
        if not already_used:
            return doc.id, data

    return None


def try_approve_order(order_ref, order_data):
    if order_data.get("status") in ("approved", "rejected"):
        return order_data

    result = find_matching_message(order_data.get("submitted_txn_id"), order_data.get("amount"))
    if result:
        msg_id, msg_data = result
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        order_ref.update({
            "status": "approved",
            "matched_message_id": msg_id,
            "approved_at": now,
            "payer_number": msg_data.get("payer_number"),
        })
        order_data = order_ref.get().to_dict()
        send_purchase_event(order_data)

    return order_data


# ---------------------------------------------------------------------------
# SMS webhook
# ---------------------------------------------------------------------------

@app.route("/api/sms-webhook", methods=["POST"])
def sms_webhook():
    if not API_TOKEN:
        return jsonify({"status": "error", "message": "Server misconfigured: API_TOKEN not set"}), 500

    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "").strip()

    if token != API_TOKEN:
        return jsonify({"status": "error", "message": "Invalid token"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "Invalid JSON body"}), 400

    sender = data.get("sender", "")
    message = data.get("message", "")

    if not sender or not message:
        return jsonify({"status": "error", "message": "sender/message required"}), 400

    amount = extract_amount(message)
    trx_id = extract_trx_id(message)
    payer_number = extract_payer_number(message)
    received_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    doc_ref = db.collection(MESSAGES_COLLECTION).document()
    doc_ref.set({
        "sender": sender,
        "message": message,
        "amount": amount,
        "trx_id": trx_id,
        "trx_id_upper": trx_id.upper() if trx_id else None,
        "payer_number": payer_number,
        "received_at": received_at,
        "timestamp": firestore.SERVER_TIMESTAMP,
    })

    print(f"[{received_at}] New SMS from {sender}: {message}")

    if trx_id:
        waiting_orders = db.collection(ORDERS_COLLECTION) \
            .where("status", "==", "verifying") \
            .where("submitted_txn_id_upper", "==", trx_id.upper()) \
            .stream()
        for order_doc in waiting_orders:
            try_approve_order(order_doc.reference, order_doc.to_dict())

    return jsonify({"status": "ok", "amount": amount, "trx_id": trx_id}), 200


@app.route("/")
def home():
    pixel = get_pixel_config()
    pv_event_id = "pv_idx_" + uuid.uuid4().hex[:12]
    return render_template("index.html", pixel_id=pixel["pixel_id"], pv_event_id=pv_event_id)


@app.route("/dashboard")
def dashboard():
    docs = db.collection(MESSAGES_COLLECTION) \
        .order_by("timestamp", direction=firestore.Query.DESCENDING) \
        .limit(100) \
        .stream()

    rows = []
    for doc in docs:
        row = doc.to_dict()
        row["id"] = doc.id
        rows.append(row)

    return render_template("dashboard.html", messages=rows)


@app.route("/api/messages", methods=["GET"])
def list_messages():
    docs = db.collection(MESSAGES_COLLECTION) \
        .order_by("timestamp", direction=firestore.Query.DESCENDING) \
        .limit(100) \
        .stream()

    result = []
    for doc in docs:
        row = doc.to_dict()
        row["id"] = doc.id
        row.pop("timestamp", None)
        result.append(row)

    return jsonify(result)


# ---------------------------------------------------------------------------
# Customer-facing order/checkout routes
# ---------------------------------------------------------------------------

@app.route("/order")
def order_page():
    product_id = request.args.get("product", "").strip()
    product = PRODUCTS.get(product_id)
    pixel = get_pixel_config()

    return render_template(
        "checkout.html",
        product_id=product_id,
        product=product,
        pixel_id=pixel["pixel_id"],
    )


@app.route("/api/orders/create", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}
    product_id = (data.get("product_id") or "").strip()
    payment_method = data.get("payment_method", "")
    fbp = (data.get("fbp") or "").strip() or None
    fbc = (data.get("fbc") or "").strip() or None
    external_id = (data.get("external_id") or "").strip() or None

    product = PRODUCTS.get(product_id)
    if not product:
        return jsonify({"status": "error", "message": "Invalid product"}), 400

    current_numbers = get_payment_numbers()
    if payment_method not in current_numbers:
        return jsonify({"status": "error", "message": "Invalid payment method"}), 400

    order_id = "ORD-" + uuid.uuid4().hex[:10].upper()
    amount = f"{product['price']:.2f}"
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    customer_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    user_agent = request.headers.get("User-Agent", "")

    db.collection(ORDERS_COLLECTION).document(order_id).set({
        "order_id": order_id,
        "product_id": product_id,
        "product_name": product["name"],
        "amount": amount,
        "payment_method": payment_method,
        "submitted_txn_id": None,
        "submitted_txn_id_upper": None,
        "matched_message_id": None,
        "status": "pending",
        "created_at": created_at,
        "approved_at": None,
        "customer_ip": customer_ip,
        "user_agent": user_agent,
        "customer_fbp": fbp,
        "customer_fbc": fbc,
        "customer_external_id": external_id,
        "timestamp": firestore.SERVER_TIMESTAMP,
    })

    return jsonify({
        "status": "ok",
        "order_id": order_id,
        "product_name": product["name"],
        "amount": amount,
        "payment_method": payment_method,
        "payment_number": current_numbers[payment_method],
    })


@app.route("/api/orders/verify", methods=["POST"])
def verify_order():
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "").strip()
    txn_id = data.get("txn_id", "").strip()

    if not order_id or not txn_id:
        return jsonify({"status": "error", "message": "order_id and txn_id required"}), 400

    order_ref = db.collection(ORDERS_COLLECTION).document(order_id)
    order_snap = order_ref.get()
    if not order_snap.exists:
        return jsonify({"status": "error", "message": "Order not found"}), 404

    order_data = order_snap.to_dict()

    if order_data["status"] == "approved":
        return jsonify({"status": "approved", "order_id": order_id})
    if order_data["status"] == "rejected":
        return jsonify({"status": "rejected", "order_id": order_id})

    order_ref.update({
        "submitted_txn_id": txn_id,
        "submitted_txn_id_upper": txn_id.upper(),
        "status": "verifying",
    })
    order_data = order_ref.get().to_dict()
    order_data = try_approve_order(order_ref, order_data)

    return jsonify({"status": order_data["status"], "order_id": order_id})


@app.route("/api/orders/status/<order_id>")
def order_status(order_id):
    order_snap = db.collection(ORDERS_COLLECTION).document(order_id).get()
    if not order_snap.exists:
        return jsonify({"status": "error", "message": "Order not found"}), 404

    order_data = order_snap.to_dict()
    return jsonify({
        "status": order_data["status"],
        "order_id": order_data["order_id"],
        "product_name": order_data["product_name"],
        "amount": order_data["amount"],
    })


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return func(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password and password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_panel"))
        error = "Wrong password"
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


def _fetch_orders_by_status(statuses):
    docs = db.collection(ORDERS_COLLECTION).where("status", "in", statuses).stream()
    rows = [doc.to_dict() for doc in docs]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def _calculate_earnings(approved_orders):
    """Approved অর্ডারগুলোর amount যোগ করে lifetime earning বের করে,
    আর approved_at-এর date আজকের date-এর সাথে মিললে সেটা today's
    earning-এ যোগ করে। today's earning প্রতিদিন নিজে থেকেই আপডেট হয়,
    কোনো manual reset লাগে না - কারণ এটা প্রতিবার request-এ আজকের
    date filter করে বের করা হয়, DB-তে আলাদা করে store করা হয় না।"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    total = 0.0
    today_total = 0.0
    for o in approved_orders:
        try:
            amt = float(o.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        total += amt

        approved_at = o.get("approved_at") or ""
        if approved_at.startswith(today_str):
            today_total += amt

    return {
        "lifetime_earning": f"{total:.2f}",
        "today_earning": f"{today_total:.2f}",
    }


@app.route("/admin/api/orders")
@admin_required
def admin_api_orders():
    pending = _fetch_orders_by_status(["pending", "verifying"])
    approved = _fetch_orders_by_status(["approved"])
    rejected = _fetch_orders_by_status(["rejected"])
    earnings = _calculate_earnings(approved)

    return jsonify({
        "pending_count": len(pending),
        "approved_count": len(approved),
        "rejected_count": len(rejected),
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "lifetime_earning": earnings["lifetime_earning"],
        "today_earning": earnings["today_earning"],
    })


@app.route("/admin")
@admin_required
def admin_panel():
    pending = _fetch_orders_by_status(["pending", "verifying"])
    approved = _fetch_orders_by_status(["approved"])
    rejected = _fetch_orders_by_status(["rejected"])
    numbers = get_payment_numbers()
    earnings = _calculate_earnings(approved)
    pixel = get_pixel_config()

    return render_template(
        "admin.html",
        pending=pending,
        approved=approved,
        rejected=rejected,
        pending_count=len(pending),
        approved_count=len(approved),
        rejected_count=len(rejected),
        bkash_number=numbers["bkash"],
        nagad_number=numbers["nagad"],
        lifetime_earning=earnings["lifetime_earning"],
        today_earning=earnings["today_earning"],
        pixel_id=pixel["pixel_id"],
        pixel_token=pixel["pixel_token"],
        pixel_connected=bool(pixel["pixel_id"] and pixel["pixel_token"]),
    )


@app.route("/admin/settings/numbers", methods=["POST"])
@admin_required
def admin_save_numbers():
    current = get_payment_numbers()
    bkash_number = request.form.get("bkash_number", "").strip() or current["bkash"]
    nagad_number = request.form.get("nagad_number", "").strip() or current["nagad"]
    set_payment_numbers(bkash_number, nagad_number)
    return redirect(url_for("admin_panel"))


@app.route("/admin/orders/<order_id>/approve", methods=["POST"])
@admin_required
def admin_approve(order_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    order_ref = db.collection(ORDERS_COLLECTION).document(order_id)
    order_ref.update({
        "status": "approved",
        "approved_at": now,
    })
    order_data = order_ref.get().to_dict()
    send_purchase_event(order_data)
    return redirect(url_for("admin_panel"))


@app.route("/admin/orders/<order_id>/reject", methods=["POST"])
@admin_required
def admin_reject(order_id):
    db.collection(ORDERS_COLLECTION).document(order_id).update({"status": "rejected"})
    return redirect(url_for("admin_panel"))


@app.route("/admin/pixel/save", methods=["POST"])
@admin_required
def admin_save_pixel():
    pixel_id = request.form.get("pixel_id", "").strip()
    pixel_token = request.form.get("pixel_token", "").strip()
    set_pixel_config(pixel_id, pixel_token)
    return redirect(url_for("admin_panel"))


@app.route("/admin/history/reset", methods=["POST"])
@admin_required
def admin_reset_history():
    """Pending, approved, rejected - সব অর্ডার Firestore থেকে
    permanently ডিলিট করে দেয়। এটা undo করা যায় না।"""
    docs = db.collection(ORDERS_COLLECTION).stream()
    deleted = 0
    for doc in docs:
        doc.reference.delete()
        deleted += 1
    print(f"Admin reset: deleted {deleted} order(s) from the ledger.")
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Dashboard: http://localhost:{port}")
    print(f"Webhook endpoint: http://<PC-IP>:{port}/api/sms-webhook")
    app.run(host="0.0.0.0", port=port, debug=False)