// const fs = require('fs');
// const path = require('path');

// let kv = null;
// try {
//   ({ kv } = require('@vercel/kv'));
// } catch (_) {
//   kv = null;
// }

// const DB_FILE = process.env.TIER1_DB_FILE || 'tier1_access.json';
// const DB_PATH = path.join(process.cwd(), DB_FILE);

// function normalizeEmail(email) {
//   if (!email || typeof email !== 'string') return null;
//   return email.trim().toLowerCase();
// }

// async function loadFileDb() {
//   try {
//     const raw = await fs.promises.readFile(DB_PATH, 'utf8');
//     const data = JSON.parse(raw);
//     if (data && typeof data === 'object') {
//       data.users = data.users || {};
//       data.purchases = data.purchases || {};
//       return data;
//     }
//   } catch (_) {}
//   return { users: {}, purchases: {} };
// }

// async function saveFileDb(db) {
//   const tmpPath = DB_PATH + '.tmp';
//   await fs.promises.writeFile(tmpPath, JSON.stringify(db, null, 2));
//   await fs.promises.rename(tmpPath, DB_PATH);
// }

// function kvReady() {
//   return !!kv && !!process.env.KV_REST_API_URL && !!process.env.KV_REST_API_TOKEN;
// }

// async function kvGet(key) {
//   return kv.get(key);
// }
// async function kvSet(key, value) {
//   return kv.set(key, value);
// }

// async function grantTier1(email, customerId, purchaseId) {
//   const normalized = normalizeEmail(email);
//   if (!normalized) return false;

//   if (kvReady()) {
//     // Idempotency on purchase
//     if (purchaseId) {
//       const existing = await kvGet(`tier1:purchase:${purchaseId}`);
//       if (existing) return false;
//       await kvSet(`tier1:purchase:${purchaseId}`, {
//         email: normalized,
//         customerId: customerId || null,
//         timestamp: new Date().toISOString()
//       });
//     }
//     const userKey = `tier1:user:${normalized}`;
//     const user = (await kvGet(userKey)) || { email: normalized, active: false, purchases: [], revocations: [] };
//     let changed = false;
//     if (customerId && user.customerId !== customerId) {
//       user.customerId = customerId;
//       changed = true;
//     }
//     if (!user.active) {
//       user.active = true;
//       changed = true;
//     }
//     if (purchaseId) {
//       user.purchases = Array.isArray(user.purchases) ? user.purchases : [];
//       user.purchases.push({ purchaseId, timestamp: new Date().toISOString() });
//       changed = true;
//     }
//     await kvSet(userKey, user);
//     return changed;
//   }

//   // File fallback (dev/local). Not persistent on Vercel.
//   const db = await loadFileDb();
//   if (purchaseId && db.purchases[purchaseId]) {
//     return false;
//   }
//   const user = db.users[normalized] || {
//     email: normalized,
//     active: false,
//     customer_id: null,
//     purchases: [],
//     revocations: []
//   };
//   let changed = false;
//   if (purchaseId) {
//     db.purchases[purchaseId] = {
//       email: normalized,
//       customer_id: customerId || null,
//       timestamp: new Date().toISOString()
//     };
//     user.purchases.push({ purchase_id: purchaseId, timestamp: new Date().toISOString() });
//     changed = true;
//   }
//   if (customerId && user.customer_id !== customerId) {
//     user.customer_id = customerId;
//     changed = true;
//   }
//   if (!user.active) { user.active = true; changed = true; }
//   db.users[normalized] = user;
//   await saveFileDb(db);
//   return changed;
// }

// async function revokeTier1(email, reason = 'revoked') {
//   const normalized = normalizeEmail(email);
//   if (!normalized) return false;

//   if (kvReady()) {
//     const userKey = `tier1:user:${normalized}`;
//     const user = (await kvGet(userKey)) || { email: normalized, active: false, purchases: [], revocations: [] };
//     let changed = false;
//     if (user.active) { user.active = false; changed = true; }
//     user.revocations = Array.isArray(user.revocations) ? user.revocations : [];
//     user.revocations.push({ reason, timestamp: new Date().toISOString() });
//     await kvSet(userKey, user);
//     return changed;
//   }

//   const db = await loadFileDb();
//   const user = db.users[normalized] || {
//     email: normalized,
//     active: false,
//     customer_id: null,
//     purchases: [],
//     revocations: []
//   };
//   let changed = false;
//   if (user.active) { user.active = false; changed = true; }
//   user.revocations.push({ reason, timestamp: new Date().toISOString() });
//   db.users[normalized] = user;
//   await saveFileDb(db);
//   return changed;
// }

// module.exports = { grantTier1, revokeTier1 };
const Stripe = require('stripe');
const stripe = new Stripe(process.env.STRIPE_SECRET_KEY, { apiVersion: '2024-06-20' });

function norm(email) {
  return (email || '').toString().trim().toLowerCase() || null;
}

async function findOrCreateCustomerByEmail(email) {
  const e = norm(email);
  if (!e) return null;
  const list = await stripe.customers.list({ email: e, limit: 1 });
  if (list.data[0]) return list.data[0].id;
  const c = await stripe.customers.create({ email: e });
  return c.id;
}

// Mark Tier-1 active on the Stripe customer record
async function grantTier1(email, customerId, purchaseId) {
  const e = norm(email);
  if (!e) return false;

  const cid = customerId || await findOrCreateCustomerByEmail(e);
  if (!cid) return false;

  await stripe.customers.update(cid, {
    metadata: {
      tier1: 'active',
      tier1_since: String(Date.now()),
      last_payment_id: purchaseId || ''
    }
  });
  return true;
}

// Mark Tier-1 revoked
async function revokeTier1(email, reason = 'revoked') {
  const e = norm(email);
  if (!e) return false;

  // find by email (ok for small/med scale)
  const list = await stripe.customers.list({ email: e, limit: 1 });
  const cid = list.data[0]?.id;
  if (!cid) return false;

  await stripe.customers.update(cid, {
    metadata: {
      tier1: 'revoked',
      revoke_reason: reason,
      tier1_revoked_at: String(Date.now())
    }
  });
  return true;
}

// Used by your broadcaster to get recipients
async function getAllTier1Emails() {
  const out = [];
  let starting_after;
  do {
    const page = await stripe.customers.list({ limit: 100, starting_after });
    for (const c of page.data) {
      if ((c.metadata?.tier1 || '').toLowerCase() === 'active' && c.email) {
        out.push(c.email);
      }
    }
    starting_after = page.has_more ? page.data.at(-1).id : undefined;
  } while (starting_after);
  return out;
}

module.exports = { grantTier1, revokeTier1, getAllTier1Emails };
