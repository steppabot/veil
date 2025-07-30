import os
import stripe
import psycopg2
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone
import requests

load_dotenv()

app = Flask(__name__)

# Stripe setup
stripe.api_key = os.getenv("STRIPE_TEST_KEY")
endpoint_secret = os.getenv("STRIPE_TEST_WEBHOOK")

SUPPORT_WEBHOOK = os.getenv("SUPPORT_WEBHOOK")

def notify_support_server(guild_id: int, tier: str):
    try:
        requests.post(SUPPORT_WEBHOOK, json={
            "content": f"🎉 Guild `{guild_id}` upgraded to **{tier.title()}** tier!"
        })
        print(f"📢 Support server notified about guild {guild_id} upgrade.")
    except Exception as e:
        print("❌ Failed to notify support server:", e)

def get_db_conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"), sslmode="require")

tier_map = {
    "price_1RowtmADYgCtNnMoK5UfUZFc": "basic",
    "price_1RoyTeADYgCtNnMolB6Za0e4": "premium",
    "price_1RoyUCADYgCtNnMomn9anPQf": "elite",
}

def apply_bonus_for_tier(guild_id, tier):
    bonus_amounts = {
        "basic": 250,
        "premium": 1000,
        "elite": 9999
    }
    bonus = bonus_amounts.get(tier)
    if not bonus:
        print(f"ℹ️ No bonus applied for tier: {tier}")
        return

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
        print("❌ Invalid payload")
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        print("❌ Invalid signature")
        return "Invalid signature", 400

    print(f"🔔 Stripe Event: {event['type']}")

    # 1️⃣ Checkout session completed → stage subscription (with renews_at)
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        discord_user_id = session.get("client_reference_id")
        guild_id = session.get("metadata", {}).get("guild_id")

        price_id = None
        # Stripe Checkout v2 structure
        if session.get("display_items"):
            price_id = session["display_items"][0].get("price", {}).get("id")
        if not price_id:
            price_id = session.get("metadata", {}).get("price_id")

        subscription_tier = tier_map.get(price_id)
        subscription_id = session.get("subscription")

        # ✅ Fetch subscription to get current_period_end for renews_at
        renews_at = None
        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                period_end = sub.get("current_period_end")
                renews_at = datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None
            except Exception as e:
                print(f"⚠️ Could not retrieve subscription {subscription_id}: {e}")

        print("🧾 Checkout Info:")
        print("  client_reference_id:", discord_user_id)
        print("  guild_id:", guild_id)
        print("  price_id:", price_id)
        print("  subscription_tier:", subscription_tier)
        print("  subscription_id:", subscription_id)
        print("  renews_at:", renews_at)

        # Stage subscription with real renews_at
        if subscription_tier and guild_id:
            try:
                with get_db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute('''
                            INSERT INTO veil_subscriptions (guild_id, tier, subscribed_at, renews_at, subscription_id)
                            VALUES (%s, %s, NOW(), %s, %s)
                            ON CONFLICT (guild_id) DO UPDATE
                            SET tier = EXCLUDED.tier,
                                subscribed_at = NOW(),
                                renews_at = EXCLUDED.renews_at,
                                subscription_id = EXCLUDED.subscription_id
                        ''', (guild_id, subscription_tier, renews_at, subscription_id))
                        conn.commit()
                        print(f"✅ Staged subscription: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")

                apply_bonus_for_tier(guild_id, subscription_tier)
                notify_support_server(guild_id, subscription_tier)

            except Exception as e:
                print("❌ DB error during checkout.session.completed:", e)

    # 2️⃣ Invoice payment succeeded → finalize subscription (populate renews_at)
    elif event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        subscription_id = invoice.get("subscription")

        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                price_id = sub["items"]["data"][0]["price"]["id"]
                guild_id = sub.get("metadata", {}).get("guild_id")
                subscription_tier = tier_map.get(price_id)

                period_end = sub.get("current_period_end")
                renews_at = datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None

                print(f"♻️ Finalizing subscription: guild={guild_id}, tier={subscription_tier}, renews_at={renews_at}")

                if subscription_tier and guild_id:
                    with get_db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute('''
                                UPDATE veil_subscriptions
                                SET tier = %s,
                                    subscribed_at = NOW(),
                                    renews_at = %s
                                WHERE guild_id = %s
                            ''', (subscription_tier, renews_at, guild_id))
                            conn.commit()
                            print(f"✅ Subscription finalized: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")

                    apply_bonus_for_tier(guild_id, subscription_tier)
                    notify_support_server(guild_id, subscription_tier)

            except Exception as e:
                print("❌ DB error during invoice.payment_succeeded:", e)

    # 3️⃣ Subscription canceled
    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        guild_id = sub.get("metadata", {}).get("guild_id")
        if guild_id:
            with get_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE veil_subscriptions
                        SET tier = 'free',
                            subscribed_at = NOW(),
                            renews_at = NULL
                        WHERE guild_id = %s
                    """, (guild_id,))
                    conn.commit()
                    print(f"❌ Subscription canceled: guild {guild_id} downgraded to free")

    # 4️⃣ Payment failed → fallback to free
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        subscription_id = invoice.get("subscription")

        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                guild_id = sub.get("metadata", {}).get("guild_id")

                if guild_id:
                    with get_db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute('''
                                UPDATE veil_subscriptions
                                SET tier = 'free',
                                    subscribed_at = NOW(),
                                    renews_at = NULL,
                                    payment_failed = TRUE
                                WHERE guild_id = %s
                            ''', (guild_id,))
                            conn.commit()
                            print(f"⚠️ Payment failed: Reverted guild {guild_id} to free tier")

            except Exception as e:
                print("❌ DB error on payment_failed:", e)

    return jsonify(success=True)

@app.route("/")
def home():
    return "VeilBot Stripe Webhook Active!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
