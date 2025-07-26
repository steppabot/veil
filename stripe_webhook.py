import os
import stripe
import psycopg2
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_TEST_KEY")
endpoint_secret = os.getenv("STRIPE_TEST_WEBHOOK")

# Helper to get fresh DB connection
def get_db_conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"), sslmode="require")

# Tier mapping
tier_map = {
    "price_1RowtmADYgCtNnMoK5UfUZFc": "basic",
    "price_1RoUhOADYgCtNnMo4sUwjusM": "premium",
    "price_1RoUocADYgCtNnMo84swUnP1": "elite",
}

# 🔁 Bonus helper
def apply_bonus_for_tier(guild_id, tier):
    bonus_amounts = {
        "basic": 250,
        "premium": 500
    }
    bonus = bonus_amounts.get(tier)
    if not bonus:
        return  # Skip for free or elite

    now = datetime.now(timezone.utc)
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE veil_users
                    SET coins = coins + %s,
                        last_refill = %s
                    WHERE guild_id = %s
                """, (bonus, now, guild_id))
                conn.commit()
                print(f"💰 Bonus coins applied: +{bonus} to all users in guild {guild_id}")
    except Exception as e:
        print("❌ Failed to apply bonus coins:", e)

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

    # ✅ Handle checkout completion
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        discord_user_id = session.get("client_reference_id")
        guild_id = session.get("metadata", {}).get("guild_id")

        price_id = session.get("display_items", [{}])[0].get("price", {}).get("id")
        if not price_id:
            price_id = session.get("metadata", {}).get("price_id")

        subscription_tier = tier_map.get(price_id)

        print("🧾 Stripe Session Info:")
        print("  client_reference_id (user_id):", discord_user_id)
        print("  guild_id:", guild_id)
        print("  price_id:", price_id)
        print("  subscription_tier:", subscription_tier)

        # 🕓 Fetch subscription renew date
        try:
            subscription_id = session.get("subscription")
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                print("🔍 Stripe Subscription Object:", sub)

                items = sub.get("items", {}).get("data", [])
                if items and items[0].get("current_period_end"):
                    period_end = items[0]["current_period_end"]
                    renews_at = datetime.fromtimestamp(period_end, tz=timezone.utc)
                    print(f"✅ Parsed renew date: {renews_at}")
                else:
                    renews_at = None
                    print("⚠️ Subscription item missing current_period_end")
        except Exception as sub_err:
            renews_at = None
            print("⚠️ Could not fetch subscription:", sub_err)

        if subscription_tier and guild_id:
            try:
                with get_db_conn() as conn:
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

                # 🪙 Apply bonus coins after successful subscription update
                apply_bonus_for_tier(guild_id, subscription_tier)

            except Exception as e:
                print("❌ DB error:", e)
                print("⚠️ Data was — guild_id:", guild_id, "tier:", subscription_tier)
                return "Database error", 500

    # ✅ Handle renewals via invoice payment
    elif event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        subscription_id = invoice.get("subscription")

        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                price_id = sub.get("items", {}).get("data", [])[0].get("price", {}).get("id")
                guild_id = sub.get("metadata", {}).get("guild_id")
                subscription_tier = tier_map.get(price_id)

                period_end = sub.get("current_period_end")
                renews_at = datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None

                if subscription_tier and guild_id:
                    with get_db_conn() as conn:
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
                            print(f"✅ Renewed subscription: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")

                    # 🪙 Apply bonus coins on renewal
                    apply_bonus_for_tier(guild_id, subscription_tier)

            except Exception as e:
                print("❌ DB error during renewal:", e)

    return jsonify(success=True)

@app.route("/")
def home():
    return "VeilBot Stripe Webhook Active!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
