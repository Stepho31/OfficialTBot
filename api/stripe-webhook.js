// const Stripe = require('stripe');
// const getRawBody = require('raw-body');
// const { grantTier1, revokeTier1 } = require('../lib/access');

// const stripe = new Stripe(process.env.STRIPE_SECRET_KEY, {
//   apiVersion: '2024-06-20',
// });

// async function extractEmailAndIds(obj) {
//   let email = null;
//   let customerId = null;
//   let purchaseId = null;

//   try {
//     if (obj && typeof obj === 'object') {
//       if (obj.customer_details && obj.customer_details.email) email = obj.customer_details.email;
//       if (!email && obj.customer_email) email = obj.customer_email;
//       if (obj.customer) customerId = obj.customer;
//       if (obj.id) purchaseId = obj.id;

//       const charges = obj.charges && obj.charges.data ? obj.charges.data : [];
//       if (!email && Array.isArray(charges) && charges[0]) {
//         const ch = charges[0];
//         if (ch.billing_details && ch.billing_details.email) email = ch.billing_details.email;
//         if (!email && ch.receipt_email) email = ch.receipt_email;
//         if (!customerId && ch.customer) customerId = ch.customer;
//         if (!purchaseId && (ch.payment_intent || ch.id)) purchaseId = ch.payment_intent || ch.id;
//       }

//       if (!email && obj.billing_details && obj.billing_details.email) email = obj.billing_details.email;
//       if (!email && obj.receipt_email) email = obj.receipt_email;
//       if (!customerId && obj.customer) customerId = obj.customer;
//     }
//   } catch (_) {}

//   return { email, customerId, purchaseId };
// }

// module.exports = async function handler(req, res) {
//   if (req.method !== 'POST') {
//     res.statusCode = 405;
//     return res.end('Method Not Allowed');
//   }

//   const sig = req.headers['stripe-signature'];
//   let event;

//   try {
//     const buf = await getRawBody(req);
//     event = stripe.webhooks.constructEvent(buf, sig, process.env.STRIPE_WEBHOOK_SECRET);
//   } catch (err) {
//     res.statusCode = 400;
//     return res.end(`Webhook Error: ${err.message}`);
//   }

//   const type = event.type;
//   const data = event.data && event.data.object ? event.data.object : {};

//   try {
//     if (type === 'checkout.session.completed') {
//       const { email, customerId, purchaseId } = await extractEmailAndIds(data);
//       if (email) await grantTier1(email, customerId, purchaseId);
//       res.statusCode = 200; return res.end('ok');
//     }

//     if (type === 'payment_intent.succeeded') {
//       const { email, customerId, purchaseId } = await extractEmailAndIds(data);
//       if (email) await grantTier1(email, customerId, purchaseId);
//       res.statusCode = 200; return res.end('ok');
//     }

//     if (type === 'charge.refunded') {
//       const { email } = await extractEmailAndIds(data);
//       if (email) await revokeTier1(email, 'refund');
//       res.statusCode = 200; return res.end('ok');
//     }

//     if (type === 'charge.dispute.created') {
//       try {
//         const chargeId = data && data.charge ? data.charge : null;
//         if (chargeId) {
//           const ch = await stripe.charges.retrieve(chargeId);
//           const { email } = await extractEmailAndIds(ch);
//           if (email) await revokeTier1(email, 'dispute');
//         }
//       } catch (_) {}
//       res.statusCode = 200; return res.end('ok');
//     }

//     res.statusCode = 200; return res.end('ignored');
//   } catch (e) {
//     res.statusCode = 500;
//     return res.end(`handler-error: ${e.message || 'unknown'}`);
//   }
// };
exports.config = { api: { bodyParser: false } }; // raw body required

const Stripe = require('stripe');
const getRawBody = require('raw-body');
// NOTE: two levels up from pages/api -> ../../lib/access
const { grantTier1, revokeTier1 } = require('../lib/access');

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY, { apiVersion: '2024-06-20' });

async function extractEmailAndIds(obj) {
  let email = null, customerId = null, purchaseId = null;
  try {
    if (obj?.customer_details?.email) email = obj.customer_details.email;
    if (!email && obj?.customer_email) email = obj.customer_email;
    if (obj?.customer) customerId = obj.customer;
    if (obj?.id) purchaseId = obj.id;

    const ch = obj?.charges?.data?.[0];
    if (!email && ch?.billing_details?.email) email = ch.billing_details.email;
    if (!email && ch?.receipt_email) email = ch.receipt_email;
    if (!customerId && ch?.customer) customerId = ch.customer;
    if (!purchaseId && (ch?.payment_intent || ch?.id)) purchaseId = ch.payment_intent || ch.id;

    if (!email && obj?.billing_details?.email) email = obj.billing_details.email;
    if (!email && obj?.receipt_email) email = obj.receipt_email;
    if (!customerId && obj?.customer) customerId = obj.customer;
  } catch (_) {}
  return { email, customerId, purchaseId };
}

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).end('Method Not Allowed');

  const sig = req.headers['stripe-signature'];
  try {
    const buf = await getRawBody(req);
    const event = stripe.webhooks.constructEvent(buf, sig, process.env.STRIPE_WEBHOOK_SECRET);

    const type = event.type;
    const data = event.data?.object || {};

    if (type === 'checkout.session.completed' || type === 'payment_intent.succeeded') {
      const { email, customerId, purchaseId } = await extractEmailAndIds(data);
      try { if (email) await grantTier1(email, customerId, purchaseId); } catch (e) { console.error('[grantTier1]', e?.message); }
      return res.status(200).end('ok');
    }

    if (type === 'charge.refunded' || type === 'charge.dispute.created') {
      let email = (await extractEmailAndIds(data)).email;
      if (!email && data?.charge) {
        try {
          const ch = await stripe.charges.retrieve(data.charge);
          email = (await extractEmailAndIds(ch)).email;
        } catch (_) {}
      }
      try { if (email) await revokeTier1(email, type === 'charge.refunded' ? 'refund' : 'dispute'); } catch (e) { console.error('[revokeTier1]', e?.message); }
      return res.status(200).end('ok');
    }

    return res.status(200).end('ignored');
  } catch (err) {
    // Signature failure or other parse error
    console.error('[webhook]', err?.message);
    return res.status(400).end('Webhook Error');
  }
};
