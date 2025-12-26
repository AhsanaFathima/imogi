import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------- ENV ----------------
# These values MUST exist in Render Environment Variables
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_SHOP_NAME = os.getenv("SHOPIFY_SHOP_NAME")

# Slack channels where "New Order" message may appear
CHANNELS_TO_SEARCH = [
    "C0A02M2VCTB",  # order
    "C0A068PHZMY"   # shopify-slack
]

# In-memory store to avoid duplicate reactions
# NOTE: This resets if app restarts
order_tracking = {}

# --------------------------------------------------
# 🔒 STRICT MATCH: ONLY messages like "ST.order #1234"
# --------------------------------------------------
def is_new_order_message(text, order_number):
    if not text:
        return False
    match = re.search(r"\bst\.order\s+#?(\d+)\b", text.lower())
    return bool(match and match.group(1) == order_number)


# --------------------------------------------------
# 🔍 FIND ORIGINAL "NEW ORDER" SLACK MESSAGE
# --------------------------------------------------
def find_new_order_message(order_number):
    print("🔍 Searching Slack message for order:", order_number)

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
            print("❌ Slack history fetch failed for channel:", channel)
            continue

        for msg in reversed(data.get("messages", [])):
            if is_new_order_message(msg.get("text", ""), order_number):
                print("✅ Found Slack message in channel:", channel)
                return msg["ts"], channel

    print("❌ New order Slack message NOT found")
    return None, None


# --------------------------------------------------
# 😀 ADD EMOJI REACTION TO SLACK MESSAGE
# --------------------------------------------------
def add_reaction(channel, message_ts, emoji_name):
    print(f"😀 Adding emoji reaction: :{emoji_name}:")

    resp = requests.post(
        "https://slack.com/api/reactions.add",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": channel,
            "timestamp": message_ts,
            "name": emoji_name  # emoji name WITHOUT :
        },
        timeout=10
    )

    # Log Slack API response (very important for debugging)
    print("⬅️ Slack response:", resp.json())


# --------------------------------------------------
# 🏷️ EMOJI MAPPINGS
# --------------------------------------------------
def payment_reaction(status):
    return {
        "pending": "hourglass_flowing_sand",
        "authorized": "lock",
        "paid": "white_check_mark",
        "voided": "x"
    }.get(status)


def fulfillment_reaction(status):
    return {
        "unfulfilled": "mailbox_with_no_mail",
        "fulfilled": "rocket"
    }.get(status)


def stock_reaction(status):
    if status and status.lower() == "stock available":
        return "package"
    return None


# --------------------------------------------------
# 📦 FETCH STOCK STATUS FROM SHOPIFY (ORDER METAFIELD)
# --------------------------------------------------
def fetch_stock_status(order_id):
    print("📦 Fetching stock status for order:", order_id)

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

    value = (
        resp.json()
        .get("data", {})
        .get("order", {})
        .get("metafield", {})
        .get("value")
    )

    print("📦 Stock status value:", value)
    return value


# --------------------------------------------------
# 🛒 SHOPIFY WEBHOOK ENTRY POINT
# --------------------------------------------------
@app.route("/webhook/shopify", methods=["POST"])
def shopify_webhook():
    print("📩 Shopify webhook received")

    data = request.get_json(force=True)
    order = data.get("order", data)

    print("🧾 Order ID:", order.get("id"))
    print("🧾 Order Name:", order.get("name"))

    order_number = str(order.get("name", "")).replace("#", "").strip()
    if not order_number:
        return jsonify({"error": "order number missing"}), 400

    # Step 1: Find Slack message only once per order
    if order_number not in order_tracking:
        ts, channel = find_new_order_message(order_number)
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

    # -------- PAYMENT STATUS --------
    payment = order.get("financial_status")
    if payment and payment != track["payment"]:
        print("💳 Payment status changed:", payment)
        emoji = payment_reaction(payment)
        if emoji:
            add_reaction(track["channel"], track["ts"], emoji)
        track["payment"] = payment

    # -------- FULFILLMENT STATUS --------
    fulfillment = order.get("fulfillment_status")
    if fulfillment and fulfillment != track["fulfillment"]:
        print("🚚 Fulfillment status changed:", fulfillment)
        emoji = fulfillment_reaction(fulfillment)
        if emoji:
            add_reaction(track["channel"], track["ts"], emoji)
        track["fulfillment"] = fulfillment

    # -------- STOCK STATUS --------
    stock = fetch_stock_status(order.get("id"))
    if stock and stock != track["stock"]:
        print("📦 Stock status changed:", stock)
        emoji = stock_reaction(stock)
        if emoji:
            add_reaction(track["channel"], track["ts"], emoji)
        track["stock"] = stock

    return jsonify({"ok": True}), 200


# --------------------------------------------------
# 🧪 HEALTH CHECK ENDPOINT
# --------------------------------------------------
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "tracked_orders": len(order_tracking)
    })


# --------------------------------------------------
# LOCAL RUN (Render uses Gunicorn instead)
# --------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
