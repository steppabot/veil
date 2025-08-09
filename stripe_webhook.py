import os
import stripe
import psycopg2
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone
import requests

load_dotenv()

SUPPORT_WEBHOOK = os.getenv("SUPPORT_WEBHOOK")  # Your support server's webhook URL

def notify_support_server(guild_id: int, tier: str):
    try:
        requests.post(SUPPORT_WEBHOOK, json={
            "content": f"🎉 Guild {guild_id} upgraded to **{tier.title()}** tier!"
        })
        print(f"📢 Support server notified about guild {guild_id} upgrade.")
    except Exception as e:
        print("❌ Failed to notify support server:", e)

# NEW: helper to post a coin top-up line your bot will parse
def notify_coin_topup(session_id: str, user_id: int, guild_id: int, coins: int):
    if not SUPPORT_WEBHOOK:
        return
    try:
        requests.post(SUPPORT_WEBHOOK, json={
            "content": f"[COIN_TOPUP] session_id={session_id} user_id={user_id} guild_id={guild_id} coins={coins}"
        }, timeout=5)
        print(f"📨 Posted COIN_TOPUP for session {session_id} (+{coins} coins)")
    except Exception as e:
        print("⚠️ Failed to post COIN_TOPUP:", e)

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_TEST_KEY")
endpoint_secret = os.getenv("STRIPE_TEST_WEBHOOK")

# Helper to get fresh DB connection
def get_db_conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"), sslmode="require")

# Tier mapping
tier_map = {
    "price_1RowtmADYgCtNnMoK5UfUZFc": "basic",
    "price_1RoyTeADYgCtNnMolB6Za0e4": "premium",
    "price_1RoyUCADYgCtNnMomn9anPQf": "elite",
}

# NEW: coin packs (one-time purchases)
coin_price_map = {
    "price_1RqrsMADYgCtNnMo6J1nKOyn": 100,   # $1
    "price_1RqrvxADYgCtNnMoPosFAmDn": 250,   # $2
    "price_1RqrxzADYgCtNnMoNQoG2pSO": 500,   # $3
    "price_1RqrzBADYgCtNnMoBHbF9yLJ": 1000,  # $5
}

# small helper
def to_int_or_none(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None

# 🔁 Bonus helper
def apply_bonus_for_tier(guild_id, tier):
    # ⛔ Elite gets no coin bonus
    if tier == "elite":
        print(f"⛔ Skipping bonus coins for Elite guild {guild_id}")
        return

    bonus_amounts = {
        "basic": 250,
        "premium": 1000
    }
    bonus = bonus_amounts.get(tier)
    if not bonus:
        return  # Skip if free or unknown

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

        mode = session.get("mode")  # "payment" (coins) or "subscription" (tiers)
        discord_user_id = to_int_or_none(session.get("client_reference_id"))
        md = session.get("metadata", {}) or {}
        guild_id = to_int_or_none(md.get("guild_id"))

        # Prefer what you set in metadata
        price_id = md.get("price_id")
        if not price_id:
            # legacy fallback
            price_id = session.get("display_items", [{}])[0].get("price", {}).get("id")

        subscription_tier = tier_map.get(price_id)
        subscription_id = session.get("subscription")

        print("🧾 Stripe Session Info:")
        print("  mode:", mode)
        print("  client_reference_id (user_id):", discord_user_id)
        print("  guild_id:", guild_id)
        print("  price_id:", price_id)
        print("  subscription_tier:", subscription_tier)
        print("  subscription_id:", subscription_id)

        # ---------- NEW: ONE-TIME COIN PURCHASES ----------
        if mode == "payment" and not subscription_tier:
            # amount to add: metadata.coins preferred, else by price_id map
            coins_from_meta = md.get("coins")
            coins_to_add = to_int_or_none(coins_from_meta) if coins_from_meta else coin_price_map.get(price_id, 0)

            if not (discord_user_id and guild_id and coins_to_add and coins_to_add > 0):
                print("⚠️ Missing data for coin credit:", discord_user_id, guild_id, coins_to_add, price_id)
                return jsonify(success=True)

            try:
                with get_db_conn() as conn:
                    with conn.cursor() as cur:
                        # ensure user row
                        cur.execute("""
                            INSERT INTO veil_users (user_id, guild_id, coins)
                            VALUES (%s, %s, 0)
                            ON CONFLICT (user_id, guild_id) DO NOTHING
                        """, (discord_user_id, guild_id))

                        # credit coins
                        cur.execute("""
                            UPDATE veil_users
                            SET coins = COALESCE(coins, 0) + %s
                            WHERE user_id = %s AND guild_id = %s
                        """, (coins_to_add, discord_user_id, guild_id))

                        conn.commit()

                print(f"💰 Credited +{coins_to_add} coins to user {discord_user_id} in guild {guild_id}")

                # tell the bot via your support webhook (so it can edit the purchaser's message)
                notify_coin_topup(session["id"], discord_user_id, guild_id, coins_to_add)

            except Exception as e:
                print("❌ DB error while crediting coins:", e)

            # done — don’t run the subscription logic below
            return jsonify(success=True)

        # ---------- (existing) SUBSCRIPTIONS ----------
        # 🕓 Fetch subscription renew date
        try:
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
                        # 🔄 Check for old subscription to cancel on upgrade
                        cur.execute("SELECT subscription_id FROM veil_subscriptions WHERE guild_id=%s", (guild_id,))
                        old_sub = cur.fetchone()
                        if old_sub and old_sub[0] and old_sub[0] != subscription_id:
                            try:
                                stripe.Subscription.delete(old_sub[0])
                                print(f"❌ Old subscription {old_sub[0]} canceled for upgrade")
                            except Exception as cancel_err:
                                print("⚠️ Could not cancel old subscription:", cancel_err)

                        # Insert or update subscription
                        cur.execute('''
                            INSERT INTO veil_subscriptions (guild_id, tier, subscribed_at, renews_at, subscription_id, payment_failed)
                            VALUES (%s, %s, NOW(), %s, %s, FALSE)
                            ON CONFLICT (guild_id) DO UPDATE
                            SET tier = EXCLUDED.tier,
                                subscribed_at = NOW(),
                                renews_at = EXCLUDED.renews_at,
                                subscription_id = EXCLUDED.subscription_id,
                                payment_failed = FALSE
                        ''', (guild_id, subscription_tier, renews_at, subscription_id))
                        conn.commit()
                        print(f"✅ Updated subscription: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")

                # 🪙 Apply bonus coins after successful subscription update
                apply_bonus_for_tier(guild_id, subscription_tier)
                notify_support_server(guild_id, subscription_tier)

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
                                INSERT INTO veil_subscriptions (guild_id, tier, subscribed_at, renews_at, subscription_id, payment_failed)
                                VALUES (%s, %s, NOW(), %s, %s, FALSE)
                                ON CONFLICT (guild_id) DO UPDATE
                                SET tier = EXCLUDED.tier,
                                    subscribed_at = NOW(),
                                    renews_at = EXCLUDED.renews_at,
                                    subscription_id = EXCLUDED.subscription_id,
                                    payment_failed = FALSE
                            ''', (guild_id, subscription_tier, renews_at, subscription_id))
                            conn.commit()
                            print(f"✅ Renewed subscription: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")

                    # 🪙 Apply bonus coins on renewal
                    apply_bonus_for_tier(guild_id, subscription_tier)
                    notify_support_server(guild_id, subscription_tier)

            except Exception as e:
                print("❌ DB error during renewal:", e)

    # ❌ Handle failed payment
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
                            print(f"⚠️ Payment failed: Reverted guild {guild_id} to free tier and flagged for bot notification")

            except Exception as e:
                print("❌ DB error on failed payment:", e)

    # ❌ Handle customer cancelation
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
                            renews_at = NULL,
                            subscription_id = NULL,
                            payment_failed = FALSE
                    WHERE guild_id = %s
                    """, (guild_id,))
                    conn.commit()
                    print(f"❌ Subscription canceled: guild {guild_id} downgraded to free")
    
    return jsonify(success=True)
    
@app.route("/")
def home():
    return "VeilBot Stripe Webhook Active!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
