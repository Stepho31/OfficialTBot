const Stripe = require('stripe');
const getRawBody = require('raw-body');

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY, {
  apiVersion: '2024-06-20',
});

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') {
    res.statusCode = 405;
    return res.end('Method Not Allowed');
  }

  try {
    const raw = (await getRawBody(req)).toString('utf8');
    let body = {};
    try { body = raw ? JSON.parse(raw) : {}; } catch (_) { body = {}; }

    if (!process.env.TIER1_PRICE_ID) {
      res.statusCode = 400;
      return res.end('Missing TIER1_PRICE_ID');
    }

    const proto = req.headers['x-forwarded-proto'] || 'https';
    const host = req.headers['x-forwarded-host'] || req.headers['host'];
    const baseUrl = `${proto}://${host}`;

    const session = await stripe.checkout.sessions.create({
      mode: 'payment',
      line_items: [{ price: process.env.TIER1_PRICE_ID, quantity: 1 }],
      success_url: `${baseUrl}/success?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${baseUrl}/cancel`,
      customer_creation: 'always',
      billing_address_collection: 'auto',
      allow_promotion_codes: true,
      consent_collection: { promotions: 'auto' },
      automatic_tax: { enabled: false },
      customer_email: typeof body.email === 'string' && body.email ? body.email : undefined,
    });

    res.setHeader('Content-Type', 'application/json');
    return res.status(200).end(JSON.stringify({ url: session.url, id: session.id }));
  } catch (e) {
    res.statusCode = 400;
    return res.end(typeof e?.message === 'string' ? e.message : 'Bad Request');
  }
};
