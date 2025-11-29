import os
import json
from typing import Optional
from flask import Flask, request, jsonify
import stripe

from access_control import grant_tier1, revoke_tier1

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
TIER1_PRICE_ID = os.getenv("TIER1_PRICE_ID")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/create-checkout-session")
def create_checkout_session():
    """Create a Checkout Session for Tier 1 access, ensuring email collection.
    Requires env TIER1_PRICE_ID. If using Payment Links, prefer that and skip this.
    """
    if not stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY not configured"}), 500
    if not TIER1_PRICE_ID:
        return jsonify({"error": "TIER1_PRICE_ID not configured"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": TIER1_PRICE_ID, "quantity": 1}],
            success_url=request.host_url.rstrip('/') + "/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url.rstrip('/') + "/cancel",
            customer_creation="always",
            billing_address_collection="auto",
            allow_promotion_codes=True,
            consent_collection={"promotions": "auto"},
            automatic_tax={"enabled": False},
            # Force email collection
            customer_email=request.json.get("email") if request.is_json and request.json.get("email") else None,
        )
        return jsonify({"url": session.url, "id": session.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/stripe/webhook")
def stripe_webhook():
    """Verified webhook handling purchase complete/refund/dispute events.
    Primary: checkout.session.completed → grant
    Backup: payment_intent.succeeded → grant
    Revocations: charge.refunded, charge.dispute.created → revoke
    """
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    if not WEBHOOK_SECRET:
        return ("Missing webhook secret", 500)

    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=sig_header, secret=WEBHOOK_SECRET
        )
    except Exception as e:
        return (f"Webhook verification failed: {e}", 400)

    type_ = event.get("type")
    data = event.get("data", {}).get("object", {})

    def _extract_email_and_ids(obj) -> tuple[str, Optional[str], Optional[str]]:
        email = None
        customer_id = None
        purchase_id = None

        # Try common spots
        email = (obj.get("customer_details", {}) or {}).get("email") or obj.get("customer_email")
        customer_id = obj.get("customer")
        purchase_id = obj.get("id")

        # Fallback from charge
        if not email:
            # If this object has an embedded charges list (e.g., payment_intent)
            charges = obj.get("charges") if isinstance(obj.get("charges"), dict) else None
            if charges:
                data_list = charges.get("data", [])
                if data_list:
                    charge = data_list[0]
                    billing_details = charge.get("billing_details", {})
                    email = billing_details.get("email") or charge.get("receipt_email")
                    customer_id = customer_id or charge.get("customer")
                    purchase_id = purchase_id or charge.get("payment_intent") or charge.get("id")
        if not email:
            # If this object itself is a charge (e.g., charge.refunded)
            billing_details = obj.get("billing_details") if isinstance(obj, dict) else None
            if isinstance(billing_details, dict):
                email = billing_details.get("email") or obj.get("receipt_email")
                customer_id = customer_id or obj.get("customer")
                purchase_id = purchase_id or obj.get("payment_intent") or obj.get("id")
        if not isinstance(email, str):
            email = None
        return email, customer_id, purchase_id

    try:
        if type_ == "checkout.session.completed":
            email, customer_id, purchase_id = _extract_email_and_ids(data)
            if email:
                changed = grant_tier1(email, customer_id, purchase_id)
                return ("ok", 200)
            return ("no-email", 200)

        if type_ == "payment_intent.succeeded":
            email, customer_id, purchase_id = _extract_email_and_ids(data)
            if email:
                changed = grant_tier1(email, customer_id, purchase_id)
                return ("ok", 200)
            return ("no-email", 200)

        if type_ == "charge.refunded":
            email, customer_id, purchase_id = _extract_email_and_ids(data)
            if email:
                revoke_tier1(email, reason="refund")
                return ("ok", 200)
            return ("no-email", 200)

        if type_ == "charge.dispute.created":
            # Dispute object contains charge ID; fetch charge to get email
            charge_id = data.get("charge")
            email = None
            customer_id = None
            if charge_id and stripe.api_key:
                try:
                    ch = stripe.Charge.retrieve(charge_id)
                    bd = ch.get("billing_details", {}) if isinstance(ch, dict) else {}
                    email = (bd or {}).get("email") or ch.get("receipt_email")
                    customer_id = ch.get("customer")
                except Exception:
                    email = None
            if email:
                revoke_tier1(email, reason="dispute")
                return ("ok", 200)
            return ("no-email", 200)

        # Ignore unhandled types
        return ("ignored", 200)
    except Exception as e:
        return (f"handler-error: {e}", 500)


if __name__ == "__main__":
    # For local testing only. In production, run via gunicorn/uvicorn and set proper host headers.
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
