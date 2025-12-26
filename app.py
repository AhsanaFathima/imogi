import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------- ENV ----------------
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_SHOP_NAME = os.getenv("SHOPIFY_SHOP_NAME")

CHANNELS_TO_SEARCH = [
    "C0A02M2VCTB",  # order
    "C0A068PHZMY"   # shopify-slack
]

# Prevent duplicate reactions
order_tracking = {}

# --------------------------------------------------
# 🔒 STRICT MATCH: ONLY "ST.order #1234"
# --------------------------------------------------
def is_new_order_message(text, order_number):
    if not text:
        return False
    match = re.search(r"\bst\.order\s+#?(\d+)\b", text.lower())
    return bool(match and match.group(1) == order_number)


# --------------------------------------------------
# 🔍 FIND ORIGINAL NEW ORDER MESSAGE
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
# 😀 ADD EMOJI REACTION
# --------------------------------------------------
def add_reaction(channel, message_ts, emoji_name):
    resp = requests.post(
        "https://slack.com/api/reactions.add",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": channel,
            "timestamp": message_ts,
            "name": emoji_name   # no colons
        },
        timeout=10
    )

    print("⬅️ Slack response:", resp.json())

    

# --------------------------------------------------
# 🏷️ EMOJI MAPPINGS (FINAL)
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

    # Cache Slack message reference
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

    # -------- PAYMENT REACTION --------
    payment = order.get("financial_status")
    if payment and payment != track["payment"]:
        emoji = payment_reaction(payment)
        if emoji:
            add_reaction(track["channel"], track["ts"], emoji)
        track["payment"] = payment

    # -------- FULFILLMENT REACTION --------
    fulfillment = order.get("fulfillment_status")
    if fulfillment and fulfillment != track["fulfillment"]:
        emoji = fulfillment_reaction(fulfillment)
        if emoji:
            add_reaction(track["channel"], track["ts"], emoji)
        track["fulfillment"] = fulfillment

    # -------- STOCK REACTION --------
    stock = fetch_stock_status(order.get("id"))
    if stock and stock != track["stock"]:
        emoji = stock_reaction(stock)
        if emoji:
            add_reaction(track["channel"], track["ts"], emoji)
        track["stock"] = stock

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
