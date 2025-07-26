import os
import stripe
import psycopg2
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

app = Flask(__name__)

# Stripe secrets
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

# Connect to your database
conn = psycopg2.connect(os.getenv("DATABASE_URL"), sslmode="require")

@app.route("/stripe-webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        discord_user_id = session.get("client_reference_id")
        guild_id = session.get("metadata", {}).get("guild_id")

        # Match link to tier using the actual price ID in Stripe (replace with real ones)
        price_id = session.get("display_items", [{}])[0].get("price", {}).get("id")
        if not price_id:
            price_id = session.get("metadata", {}).get("price_id")

        tier_map = {
            "price_1RoUeqADYgCtNnMoeFvB8uDf": "basic",
            "price_1RoUhOADYgCtNnMo4sUwjusM": "premium",
            "price_1RoUocADYgCtNnMo84swUnP1": "elite"
        }
        subscription_tier = tier_map.get(price_id)

        print("🧾 Stripe Session Info:")
        print("  client_reference_id (user_id):", discord_user_id)
        print("  guild_id:", guild_id)
        print("  price_id:", price_id)
        print("  subscription_tier:", subscription_tier)

        if subscription_tier and guild_id:
            try:
                # Get Stripe subscription ID
                subscription_id = session.get("subscription")
                renews_at = None

                if subscription_id:
                    try:
                        sub = stripe.Subscription.retrieve(subscription_id)
                        paid_until_unix = sub.get("current_period_end")
                        if paid_until_unix:
                            renews_at = datetime.fromtimestamp(paid_until_unix, tz=timezone.utc)
                    except Exception as sub_err:
                        print("⚠️ Failed to fetch subscription:", sub_err)

                with conn.cursor() as cur:
                    cur.execute('''
                        INSERT INTO veil_subscriptions (guild_id, tier, subscribed_at, renews_at)
                        VALUES (%s, %s, NOW(), %s)
                        ON CONFLICT (guild_id) DO UPDATE
                        SET tier = EXCLUDED.tier,
                            subscribed_at = NOW(),
                            renews_at = EXCLUDED.renews_at
                    ''', (guild_id, subscription_tier, renews_at))
                    conn.commit()
                    print(f"✅ Updated subscription: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")
            except Exception as e:
                print("❌ DB error:", e)
                print("⚠️ Data was — guild_id:", guild_id, "tier:", subscription_tier)
                return "Database error", 500

    return jsonify(success=True)

@app.route("/")
def home():
    return "VeilBot Stripe Webhook is Live ✅"
