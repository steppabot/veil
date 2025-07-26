import os
import stripe
import psycopg2
from flask import Flask, request, jsonify
from dotenv import load_dotenv

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
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    # Handle completed payment session
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        discord_user_id = session.get("client_reference_id")

        # Match link to tier using the actual price ID in Stripe (replace with real ones)
        price_id = session.get("display_items", [{}])[0].get("price", {}).get("id")
        if not price_id:
            price_id = session.get("metadata", {}).get("price_id")

        tier_map = {
            "price_1MlYkPCoOLdI6N8uXZzP1HZn": "basic",
            "price_1MlYkZCoOLdI6N8ukTSXHeEo": "premium",
            "price_1MlYkiCoOLdI6N8uhTjwA3dU": "elite"
        }

        subscription_tier = tier_map.get(price_id)
        if subscription_tier and discord_user_id:
            try:
                with conn.cursor() as cur:
                    cur.execute('''
                        INSERT INTO veil_users (user_id, guild_id, coins, veils_unveiled, subscription_tier)
                        VALUES (%s, 0, 0, 0, %s)
                        ON CONFLICT (user_id, guild_id) DO UPDATE
                        SET subscription_tier = EXCLUDED.subscription_tier
                    ''', (discord_user_id, subscription_tier))
                    conn.commit()
            except Exception as e:
                print("❌ DB error:", e)
                return "Database error", 500

    return jsonify(success=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
