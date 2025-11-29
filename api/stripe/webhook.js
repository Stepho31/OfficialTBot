const Stripe = require('stripe');
const getRawBody = require('raw-body');
const { grantTier1, revokeTier1 } = require('../../lib/access');

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY, {
  apiVersion: '2024-06-20',
});

async function extractEmailAndIds(obj) {
  let email = null;
  let customerId = null;
  let purchaseId = null;

  try {
    // Common spots
    if (obj && typeof obj === 'object') {
      if (obj.customer_details && obj.customer_details.email) email = obj.customer_details.email;
      if (!email && obj.customer_email) email = obj.customer_email;
      if (obj.customer) customerId = obj.customer;
      if (obj.id) purchaseId = obj.id;

      // Charges within PI
      const charges = obj.charges && obj.charges.data ? obj.charges.data : [];
      if (!email && Array.isArray(charges) && charges[0]) {
        const ch = charges[0];
        if (ch.billing_details && ch.billing_details.email) email = ch.billing_details.email;
        if (!email && ch.receipt_email) email = ch.receipt_email;
        if (!customerId && ch.customer) customerId = ch.customer;
        if (!purchaseId && (ch.payment_intent || ch.id)) purchaseId = ch.payment_intent || ch.id;
      }

      // If this is a bare Charge object
      if (!email && obj.billing_details && obj.billing_details.email) email = obj.billing_details.email;
      if (!email && obj.receipt_email) email = obj.receipt_email;
      if (!customerId && obj.customer) customerId = obj.customer;
    }
  } catch (_) {}

  return { email, customerId, purchaseId };
}

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') {
    res.statusCode = 405;
    return res.end('Method Not Allowed');
  }

  const sig = req.headers['stripe-signature'];
  let event;

  try {
    const buf = await getRawBody(req);
    event = stripe.webhooks.constructEvent(buf, sig, process.env.STRIPE_WEBHOOK_SECRET);
  } catch (err) {
    res.statusCode = 400;
    return res.end(`Webhook Error: ${err.message}`);
  }

  const type = event.type;
  const data = event.data && event.data.object ? event.data.object : {};

  try {
    if (type === 'checkout.session.completed') {
      const { email, customerId, purchaseId } = await extractEmailAndIds(data);
      if (email) await grantTier1(email, customerId, purchaseId);
      res.statusCode = 200; return res.end('ok');
    }

    if (type === 'payment_intent.succeeded') {
      const { email, customerId, purchaseId } = await extractEmailAndIds(data);
      if (email) await grantTier1(email, customerId, purchaseId);
      res.statusCode = 200; return res.end('ok');
    }

    if (type === 'charge.refunded') {
      const { email } = await extractEmailAndIds(data);
      if (email) await revokeTier1(email, 'refund');
      res.statusCode = 200; return res.end('ok');
    }

    if (type === 'charge.dispute.created') {
      // Need to resolve email from charge ID
      try {
        const chargeId = data && data.charge ? data.charge : null;
        if (chargeId) {
          const ch = await stripe.charges.retrieve(chargeId);
          const { email } = await extractEmailAndIds(ch);
          if (email) await revokeTier1(email, 'dispute');
        }
      } catch (_) {}
      res.statusCode = 200; return res.end('ok');
    }

    // Ignore others
    res.statusCode = 200; return res.end('ignored');
  } catch (e) {
    res.statusCode = 500;
    return res.end(`handler-error: ${e.message || 'unknown'}`);
  }
};
