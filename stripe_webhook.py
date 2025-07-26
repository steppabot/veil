import os
import stripe
import psycopg2
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

app = Flask(__name__)

# Stripe secrets
stripe.api_key = os.getenv("STRIPE_TEST_KEY")
endpoint_secret = os.getenv("STRIPE_TEST_WEBHOOK")

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

    # Handle checkout completion
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        discord_user_id = session.get("client_reference_id")
        guild_id = session.get("metadata", {}).get("guild_id")

        # Stripe price ID matching
        price_id = session.get("display_items", [{}])[0].get("price", {}).get("id")
        if not price_id:
            price_id = session.get("metadata", {}).get("price_id")

        tier_map = {
            "price_1RowtmADYgCtNnMoK5UfUZFc": "basic",
            "price_1RoyTeADYgCtNnMolB6Za0e4": "premium",
            "price_1RoyUCADYgCtNnMomn9anPQf": "elite"
        }
        
        subscription_tier = tier_map.get(price_id)

        print("🧾 Stripe Session Info:")
        print("  client_reference_id (user_id):", discord_user_id)
        print("  guild_id:", guild_id)
        print("  price_id:", price_id)
        print("  subscription_tier:", subscription_tier)

        renews_at = None
        try:
            subscription_id = session.get("subscription")
            if subscription_id:
                subscription_obj = stripe.Subscription.retrieve(subscription_id)
                renews_at_unix = subscription_obj.get("current_period_end")
                if renews_at_unix:
                    renews_at = datetime.fromtimestamp(renews_at_unix, tz=timezone.utc)
        except Exception as e:
            print("⚠️ Could not fetch subscription:", e)

        # Update DB with fresh connection
        if subscription_tier and guild_id:
            try:
                with psycopg2.connect(os.getenv("DATABASE_URL"), sslmode="require") as fresh_conn:
                    with fresh_conn.cursor() as cur:
                        cur.execute('''
                            INSERT INTO veil_subscriptions (guild_id, tier, subscribed_at, renews_at)
                            VALUES (%s, %s, NOW(), %s)
                            ON CONFLICT (guild_id) DO UPDATE
                            SET tier = EXCLUDED.tier,
                                subscribed_at = NOW(),
                                renews_at = EXCLUDED.renews_at
                        ''', (guild_id, subscription_tier, renews_at))
                        fresh_conn.commit()
                        print(f"✅ Updated subscription: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")
            except Exception as e:
                print("❌ DB error:", e)
                print("⚠️ Data was — guild_id:", guild_id, "tier:", subscription_tier)
                return "Database error", 500

    return jsonify(success=True)

@app.route("/")
def home():
    return "✅ Veil Webhook Server is Live!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
