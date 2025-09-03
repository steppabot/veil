import os
import stripe
import psycopg2
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone
import requests

load_dotenv()

SUPPORT_WEBHOOK = os.getenv("SUPPORT_WEBHOOK")  # Your support server's webhook URL
DISCORD_API_BASE = os.getenv("DISCORD_API_BASE", "https://discord.com/api/v10")

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_db_conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"), sslmode="require")

def fmt(n: int) -> str:
    return f"{n:,}"

def to_int_or_none(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None

def notify_support_server(guild_id: int, tier: str):
    if not SUPPORT_WEBHOOK:
        return
    try:
        requests.post(SUPPORT_WEBHOOK, json={
            "content": f"🎉 Guild {guild_id} upgraded to **{tier.title()}** tier!"
        }, timeout=5)
        print(f"📢 Support server notified about guild {guild_id} upgrade.")
    except Exception as e:
        print("❌ Failed to notify support server:", e)

def patch_interaction_original(application_id: int | str, interaction_token: str, payload: dict):
    """
    PATCH the original interaction message (no bot token required).
    """
    url = f"{DISCORD_API_BASE}/webhooks/{int(application_id)}/{interaction_token}/messages/@original"
    r = requests.patch(url, json=payload, timeout=8)
    print(f"[discord] PATCH @original -> {r.status_code} {r.text[:200]}")
    r.raise_for_status()

# Tier mapping
tier_map = {
    "price_1RuT1sADYgCtNnMoWMzdQ7YI": "basic",
    "price_1RuT34ADYgCtNnModSx70nr1": "premium",
    "price_1RuT3ZADYgCtNnMopSZon3vt": "elite",
}

# Coin packs (one-time purchases)
coin_price_map = {
    "price_1RuT5IADYgCtNnMorF0zsMRK": 100,   # $1
    "price_1RuT5dADYgCtNnMoNY5O0cuc": 250,   # $2
    "price_1RuT5yADYgCtNnMoWTUR4XMC": 500,   # $3
    "price_1RuT6KADYgCtNnMoKwM3iw9H": 1000,  # $5
}

def apply_bonus_for_tier(guild_id, tier):
    # ⛔ Elite gets no coin bonus
    if tier == "elite":
        print(f"⛔ Skipping bonus coins for Elite guild {guild_id}")
        return

    bonus_amounts = {"basic": 250, "premium": 1000}
    bonus = bonus_amounts.get(tier)
    if not bonus:
        return

    now = datetime.now(timezone.utc)
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE veil_users
                   SET coins = COALESCE(coins,0) + %s,
                       last_refill = %s
                 WHERE guild_id = %s
            """, (bonus, now, guild_id))
        print(f"💰 Bonus coins applied: +{bonus} to all users in guild {guild_id}")
    except Exception as e:
        print("❌ Failed to apply bonus coins:", e)

# ─────────────────────────────────────────────────────────────────────────────
# Webhook
# ─────────────────────────────────────────────────────────────────────────────

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

    # ── checkout.session.completed ────────────────────────────────────────────
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        mode = session.get("mode")  # "payment" (coins) or "subscription" (tiers)
        discord_user_id = to_int_or_none(session.get("client_reference_id"))
        md = session.get("metadata", {}) or {}
        guild_id = to_int_or_none(md.get("guild_id"))

        # Prefer metadata price_id; fall back to older structures if needed
        price_id = md.get("price_id")
        if not price_id:
            # Legacy fallback (older Checkout flows)
            line_items = session.get("display_items", [])
            if line_items and isinstance(line_items, list):
                price_id = line_items[0].get("price", {}).get("id")

        subscription_tier = tier_map.get(price_id)
        subscription_id = session.get("subscription")
        stripe_session_id = session.get("id")  # ← used to look up interaction

        print("🧾 Stripe Session Info:")
        print("  mode:", mode)
        print("  client_reference_id (user_id):", discord_user_id)
        print("  guild_id:", guild_id)
        print("  price_id:", price_id)
        print("  subscription_tier:", subscription_tier)
        print("  subscription_id:", subscription_id)
        print("  stripe_session_id:", stripe_session_id)

        # ---------- ONE-TIME COIN PURCHASES ----------
        if mode == "payment" and not subscription_tier:
            # Coins to add: metadata.coins preferred, else price map
            coins_from_meta = md.get("coins")
            coins_to_add = to_int_or_none(coins_from_meta) if coins_from_meta else coin_price_map.get(price_id, 0)

            if not (discord_user_id and guild_id and coins_to_add and coins_to_add > 0):
                print("⚠️ Missing data for coin credit:", discord_user_id, guild_id, coins_to_add, price_id)
                return jsonify(success=True)

            try:
                # 1) Credit coins and get fresh balance in a single roundtrip
                with get_db_conn() as conn, conn.cursor() as cur:
                    # Ensure row exists
                    cur.execute("""
                        INSERT INTO veil_users (user_id, guild_id, coins)
                        VALUES (%s, %s, 0)
                        ON CONFLICT (user_id, guild_id) DO NOTHING
                    """, (discord_user_id, guild_id))

                    # Credit and return new balance
                    cur.execute("""
                        UPDATE veil_users
                           SET coins = COALESCE(coins,0) + %s
                         WHERE user_id = %s AND guild_id = %s
                     RETURNING coins
                    """, (coins_to_add, discord_user_id, guild_id))
                    row = cur.fetchone()
                    new_balance = row[0] if row else None

                print(f"💰 Credited +{coins_to_add} to user {discord_user_id} in guild {guild_id}; new balance={new_balance}")

                # 2) Look up the interaction to PATCH @original (no bot token needed)
                with get_db_conn() as conn, conn.cursor() as cur:
                    cur.execute("""
                        SELECT interaction_token, application_id, user_id, guild_id, coins
                          FROM coin_checkout_sessions
                         WHERE stripe_session_id = %s
                    """, (stripe_session_id,))
                    sess_row = cur.fetchone()

                if not sess_row:
                    print(f"[coin] ⚠️ No coin_checkout_sessions row for {stripe_session_id}; cannot edit interaction.")
                    # Optional: still log to support server
                    if SUPPORT_WEBHOOK:
                        requests.post(SUPPORT_WEBHOOK, json={
                            "content": f"🪙 Credited **+{coins_to_add}** to <@{discord_user_id}> in guild `{guild_id}`, "
                                       f"but no interaction found to patch (session `{stripe_session_id}`)."
                        }, timeout=5)
                    return jsonify(success=True)

                interaction_token, application_id, u_saved, g_saved, coins_saved = sess_row

                # 3) Sanity checks
                if u_saved != discord_user_id or g_saved != guild_id:
                    print(f"[coin] id mismatch for {stripe_session_id}; saved=({u_saved},{g_saved}) got=({discord_user_id},{guild_id})")
                    return jsonify(success=True)

                # 4) Build the same embed you had in your bot
                veilcoinemoji = "🪙"  # server-side fallback; custom emoji not available here
                coins_str = fmt(coins_saved or coins_to_add)
                bal_str   = fmt(new_balance or 0)

                payload = {
                    "embeds": [{
                        "title": f"{veilcoinemoji} +{coins_str} Veil Coins Added",
                        "description": f"Thanks for your support! Your new balance is **{bal_str}**.",
                        "color": 0xeeac00,
                        "fields": [
                            {"name": "Amount",  "value": f"{veilcoinemoji} `{coins_str}`", "inline": True},
                            {"name": "Balance", "value": f"`{bal_str}`",                   "inline": True},
                        ],
                        "footer": {"text": "Tip: use /user any time to see your balance."}
                    }],
                    "components": []
                }

                # 5) PATCH the original interaction message
                try:
                    patch_interaction_original(application_id, interaction_token, payload)
                except Exception as e:
                    print(f"[coin] ❌ PATCH failed: {e}")

            except Exception as e:
                print("❌ DB error while crediting coins:", e)

            return jsonify(success=True)

        # ---------- SUBSCRIPTIONS (create/upgrade) ----------
        # (unchanged from your code, DB updates + optional notifications)
        try:
            renews_at = None
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                items = sub.get("items", {}).get("data", [])
                if items and items[0].get("current_period_end"):
                    period_end = items[0]["current_period_end"]
                    renews_at = datetime.fromtimestamp(period_end, tz=timezone.utc)

        except Exception as sub_err:
            renews_at = None
            print("⚠️ Could not fetch subscription:", sub_err)

        if subscription_tier and guild_id:
            try:
                with get_db_conn() as conn, conn.cursor() as cur:
                    # cancel old sub if needed
                    cur.execute("SELECT subscription_id FROM veil_subscriptions WHERE guild_id=%s", (guild_id,))
                    old_sub = cur.fetchone()
                    if old_sub and old_sub[0] and old_sub[0] != subscription_id:
                        try:
                            stripe.Subscription.delete(old_sub[0])
                            print(f"❌ Old subscription {old_sub[0]} canceled for upgrade")
                        except Exception as cancel_err:
                            print("⚠️ Could not cancel old subscription:", cancel_err)

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
                print(f"✅ Updated subscription: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")

                apply_bonus_for_tier(guild_id, subscription_tier)
                notify_support_server(guild_id, subscription_tier)

            except Exception as e:
                print("❌ DB error:", e)
                print("⚠️ Data was — guild_id:", guild_id, "tier:", subscription_tier)
                return "Database error", 500

    # ── invoice.payment_succeeded (renewals) ──────────────────────────────────
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
                    with get_db_conn() as conn, conn.cursor() as cur:
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
                    print(f"✅ Renewed subscription: guild_id={guild_id}, tier={subscription_tier}, renews_at={renews_at}")

                    apply_bonus_for_tier(guild_id, subscription_tier)
                    notify_support_server(guild_id, subscription_tier)

            except Exception as e:
                print("❌ DB error during renewal:", e)

    # ── invoice.payment_failed ────────────────────────────────────────────────
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        subscription_id = invoice.get("subscription")

        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                guild_id = sub.get("metadata", {}).get("guild_id")

                if guild_id:
                    with get_db_conn() as conn, conn.cursor() as cur:
                        cur.execute('''
                            UPDATE veil_subscriptions
                               SET tier = 'free',
                                   subscribed_at = NOW(),
                                   renews_at = NULL,
                                   payment_failed = TRUE
                             WHERE guild_id = %s
                        ''', (guild_id,))
                    print(f"⚠️ Payment failed: Reverted guild {guild_id} to free tier and flagged for bot notification")

            except Exception as e:
                print("❌ DB error on failed payment:", e)

    # ── customer.subscription.deleted ────────────────────────────────────────
    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        guild_id = sub.get("metadata", {}).get("guild_id")
        if guild_id:
            with get_db_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    UPDATE veil_subscriptions
                       SET tier = 'free',
                           subscribed_at = NOW(),
                           renews_at = NULL,
                           subscription_id = NULL,
                           payment_failed = FALSE
                     WHERE guild_id = %s
                """, (guild_id,))
            print(f"❌ Subscription canceled: guild {guild_id} downgraded to free")

    return jsonify(success=True)

@app.route("/")
def home():
    return "VeilBot Stripe Webhook Active!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
