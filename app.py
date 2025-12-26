import os
import re
import time
import requests
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ---------------- ENV ----------------
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_SHOP_NAME = os.getenv("SHOPIFY_SHOP_NAME")

CHANNELS_TO_SEARCH = [
    "C0A02M2VCTB",  # order
    "C0A068PHZMY"   # shopify-slack
]

# In-memory tracking (resets on restart)
order_tracking = {}

# --------------------------------------------------
# 🔒 STRICT MATCH: ONLY "ST.order #1234"
# --------------------------------------------------
def is_new_order_message(text, order_number):
    if not text:
        return False

    text = text.lower().strip()
    blacklist = ["fulfilled", "tracking", "report", "generated", "payment"]
    if any(word in text for word in blacklist):
        return False

    match = re.search(r"\bst\.order\s+#?(\d+)\b", text)
    return bool(match and match.group(1) == order_number)


# --------------------------------------------------
# 🔍 FIND NEW ORDER MESSAGE
# --------------------------------------------------
def find_new_order_message(order_number):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    for channel in CHANNELS_TO_SEARCH:
        resp = requests.get(
            "https://slack.com/api/conversations.history",
            headers=headers,
            params={"channel": channel, "limit": 100},
            timeout=10
        )

        data = resp.json()
        if not data.get("ok"):
            continue

        for msg in reversed(data.get("messages", [])):
            if is_new_order_message(msg.get("text", ""), order_number):
                return msg["ts"], channel

    return None, None


# --------------------------------------------------
# 🔁 RETRY SLACK SEARCH (for pending on creation)
# --------------------------------------------------
def find_slack_message_with_retry(order_number, retries=3, delay=5):
    for attempt in range(retries):
        ts, channel = find_new_order_message(order_number)
        if ts:
            return ts, channel
        time.sleep(delay)
    return None, None


# --------------------------------------------------
# 📤 POST THREAD MESSAGE
# --------------------------------------------------
def post_thread_message(channel, thread_ts, text):
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": channel,
            "thread_ts": thread_ts,
            "text": text
        },
        timeout=10
    )
    return resp.json().get("ok", False)


# --------------------------------------------------
# 🏷️ MESSAGE BUILDERS
# --------------------------------------------------
def payment_message(status):
    return {
        "pending": "⏳ Payment Pending",
        "authorized": "🔒 Payment Authorized",
        "paid": "✅ Payment Paid",
        "voided": "❌ Payment Voided"
    }.get(status, f"💳 Payment {status}")


def fulfillment_message(status):
    return {
        "fulfilled": "🚀 Fulfilled",
        "unfulfilled": "📭 Unfulfilled"
    }.get(status, f"📦 {status}")


def stock_message(status):
    if status.lower() == "stock available":
        return "📦 Stock Available"
    return f"📦 Stock Status: {status}"


# --------------------------------------------------
# 📦 FETCH STOCK STATUS (ORDER METAFIELD)
# --------------------------------------------------
def fetch_stock_status(order_id):
    url = f"https://{SHOPIFY_SHOP_NAME}.myshopify.com/admin/api/2025-01/graphql.json"

    query = """
    query ($id: ID!) {
      order(id: $id) {
        metafield(namespace: "custom", key: "stock_status") {
          value
        }
      }
    }
    """

    resp = requests.post(
        url,
        headers={
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json"
        },
        json={
            "query": query,
            "variables": {"id": f"gid://shopify/Order/{order_id}"}
        },
        timeout=10
    )

    return (
        resp.json()
        .get("data", {})
        .get("order", {})
        .get("metafield", {})
        .get("value")
    )


# --------------------------------------------------
# 🛒 SHOPIFY WEBHOOK
# --------------------------------------------------
@app.route("/webhook/shopify", methods=["POST"])
def shopify_webhook():
    data = request.get_json(force=True)
    order = data.get("order", data)

    order_number = str(order.get("name", "")).replace("#", "").strip()
    if not order_number:
        return jsonify({"error": "order number missing"}), 400

    # Detect metafield-only update
    is_metafield_update = bool(order.get("metafields"))

    # Find Slack message (retry for first creation)
    if order_number not in order_tracking:
        ts, channel = find_slack_message_with_retry(order_number)
        if not ts:
            return jsonify({"ok": False}), 202

        order_tracking[order_number] = {
            "ts": ts,
            "channel": channel,
            "payment": None,
            "fulfillment": None,
            "stock": None
        }

    track = order_tracking[order_number]
    time_now = datetime.now().strftime("%I:%M %p")

    # -------- PAYMENT --------
    payment_status = order.get("financial_status")
    if payment_status and payment_status != track["payment"]:
        if payment_status == "pending" or not is_metafield_update:
            if post_thread_message(
                track["channel"],
                track["ts"],
                f"{payment_message(payment_status)} • {time_now}"
            ):
                track["payment"] = payment_status

    # -------- FULFILLMENT --------
    fulfillment_status = order.get("fulfillment_status")
    if fulfillment_status and fulfillment_status != track["fulfillment"]:
        if not is_metafield_update:
            if post_thread_message(
                track["channel"],
                track["ts"],
                f"{fulfillment_message(fulfillment_status)} • {time_now}"
            ):
                track["fulfillment"] = fulfillment_status

    # -------- STOCK STATUS --------
    stock_status = fetch_stock_status(order.get("id"))
    if stock_status and stock_status != track["stock"]:
        if post_thread_message(
            track["channel"],
            track["ts"],
            f"{stock_message(stock_status)} • {time_now}"
        ):
            track["stock"] = stock_status

    return jsonify({"ok": True}), 200


# --------------------------------------------------
# 🧪 HEALTH CHECK
# --------------------------------------------------
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "tracked_orders": len(order_tracking)
    })


# --------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
