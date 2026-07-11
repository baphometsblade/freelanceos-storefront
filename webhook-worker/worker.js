/**
 * FreelanceOS Stripe Webhook Handler
 * Cloudflare Worker — ES Module format
 *
 * Handles checkout.session.completed events from Stripe:
 *   1. Verifies Stripe webhook signature
 *   2. Identifies the purchased product
 *   3. Sends a delivery email via MailChannels
 *   4. Logs the transaction to D1
 *   5. Returns 200/400 with appropriate JSON
 */

// ---------------------------------------------------------------------------
// Product catalog
// Notion URLs: go to each page in Notion → Share → Publish to web → confirm
// the page is publicly accessible, then the URL below should work for buyers.
// If buyers see a login page, the template page is not yet published to web.
// ---------------------------------------------------------------------------
const PRODUCTS = {
  'FreelanceOS Pro': {
    notion_url: 'https://www.notion.so/33eec0f953c480cea52ce1a1dc856caf',
    notion_url_2: null,
    pdf_url: 'https://baphometsblade.github.io/freelanceos-storefront/downloads/freelanceos-pro-guide.pdf',
    price_amount: 2900,
    name: 'FreelanceOS Pro',
    description: 'Complete Notion workspace for freelancers',
    stripe_product_id: null, // TODO: confirm prod_xxx from Stripe dashboard → Products
  },
  'FreelanceOS Quick Start': {
    notion_url: 'https://www.notion.so/33cec0f953c4803ab41bf4d32820e9fe',
    notion_url_2: null,
    pdf_url: null,
    price_amount: 900,
    name: 'FreelanceOS Quick Start',
    description: '3-Database Starter Kit for freelancers',
    stripe_product_id: 'prod_UStEgKWq8MapsL',
  },
  'CreatorHQ Pro': {
    notion_url: 'https://www.notion.so/34bec0f953c480479378e2a6ac05d08a',
    notion_url_2: null,
    pdf_url: null,
    price_amount: 2900,
    name: 'CreatorHQ Pro',
    description: 'Content creator command center in Notion',
    stripe_product_id: null, // TODO: confirm prod_xxx from Stripe dashboard → Products
  },
  'Ultimate Creator Bundle': {
    notion_url: 'https://www.notion.so/33eec0f953c480cea52ce1a1dc856caf',
    notion_url_2: 'https://www.notion.so/34bec0f953c480479378e2a6ac05d08a',
    pdf_url: null,
    price_amount: 4900,
    name: 'FreelanceOS Pro + CreatorHQ Pro Bundle',
    description: 'Both premium Notion template systems',
    stripe_product_id: 'prod_USCv63WOhsqWRr',
  },
  'SoloFounderOS Pro': {
    notion_url: 'TODO_ADD_SOLOFOUNDEROS_NOTION_URL',
    notion_url_2: null,
    pdf_url: null,
    price_amount: 3900,
    name: 'SoloFounderOS Pro',
    description: 'Notion OS for indie hackers & solopreneurs',
    stripe_product_id: 'prod_UrSUMtHU0YAjo6',
  },
  'AgencyOS Pro': {
    notion_url: 'TODO_ADD_AGENCYOS_NOTION_URL',
    notion_url_2: null,
    pdf_url: null,
    price_amount: 4900,
    name: 'AgencyOS Pro',
    description: 'Notion system for agency owners',
    stripe_product_id: null, // TODO: find prod_xxx from Stripe dashboard
  },
};

// Index by Stripe product ID for O(1) lookup
const PRODUCT_BY_STRIPE_ID = {};
for (const [key, product] of Object.entries(PRODUCTS)) {
  if (product.stripe_product_id) {
    PRODUCT_BY_STRIPE_ID[product.stripe_product_id] = product;
  }
}

// Support email shown in delivery emails
const SUPPORT_EMAIL = 'markmma1985@gmail.com';
const FROM_EMAIL = 'noreply@freelanceos.site'; // must match a domain you own / have SPF for
const FROM_NAME = 'FreelanceOS';

// ---------------------------------------------------------------------------
// Main fetch handler
// ---------------------------------------------------------------------------
export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response(JSON.stringify({ error: 'Method not allowed' }), {
        status: 405,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // Read raw body — needed for signature verification
    const rawBody = await request.text();

    // -----------------------------------------------------------------------
    // 1. Verify Stripe webhook signature
    // -----------------------------------------------------------------------
    const signature = request.headers.get('stripe-signature');
    if (!signature) {
      console.error('Missing stripe-signature header');
      return jsonResponse({ error: 'Missing stripe-signature' }, 400);
    }

    let event;
    try {
      event = await verifyStripeSignature(rawBody, signature, env.STRIPE_WEBHOOK_SECRET);
    } catch (err) {
      console.error('Signature verification failed:', err.message);
      return jsonResponse({ error: 'Invalid signature' }, 400);
    }

    // -----------------------------------------------------------------------
    // 2. Only handle checkout.session.completed
    // -----------------------------------------------------------------------
    if (event.type !== 'checkout.session.completed') {
      return jsonResponse({ received: true, action: 'ignored', type: event.type }, 200);
    }

    const session = event.data.object;
    const sessionId = session.id;
    const customerEmail = session.customer_details?.email || session.customer_email;
    const amountPaid = session.amount_total;
    const currency = session.currency;

    console.log(`Processing checkout.session.completed: ${sessionId} | email: ${customerEmail} | amount: ${amountPaid}`);

    // -----------------------------------------------------------------------
    // 3. Idempotency check — bail out if already delivered
    // -----------------------------------------------------------------------
    try {
      const existing = await env.DB.prepare(
        'SELECT id, delivery_status FROM deliveries WHERE stripe_session_id = ?'
      )
        .bind(sessionId)
        .first();

      if (existing) {
        console.log(`Session ${sessionId} already processed (status: ${existing.delivery_status}). Skipping.`);
        return jsonResponse({ received: true, action: 'duplicate', session_id: sessionId }, 200);
      }
    } catch (dbErr) {
      console.error('D1 idempotency check failed:', dbErr.message);
      // Non-fatal — continue so the customer isn't left without their product
    }

    // -----------------------------------------------------------------------
    // 4. Identify the product
    // -----------------------------------------------------------------------
    let product = null;

    // Strategy A: match by Stripe product ID from line items metadata
    // (requires the session to have been expanded — Stripe sends product_id
    //  in line_items if you expand it, but the webhook payload includes
    //  metadata.product_id if you set it on the payment link)
    const metaProductId = session.metadata?.product_id;
    if (metaProductId && PRODUCT_BY_STRIPE_ID[metaProductId]) {
      product = PRODUCT_BY_STRIPE_ID[metaProductId];
    }

    // Strategy B: match by product name in metadata
    if (!product && session.metadata?.product_name) {
      product = findProductByName(session.metadata.product_name);
    }

    // Strategy C: fetch line items from Stripe API to get product ID
    if (!product && env.STRIPE_SECRET_KEY) {
      try {
        product = await resolveProductFromLineItems(sessionId, env.STRIPE_SECRET_KEY);
      } catch (err) {
        console.error('Failed to resolve product from line items:', err.message);
      }
    }

    // Strategy D: fallback — match by amount paid
    if (!product) {
      product = findProductByAmount(amountPaid);
    }

    if (!product) {
      console.error(`Unknown product for session ${sessionId}. Amount: ${amountPaid}, metadata: ${JSON.stringify(session.metadata)}`);
      await logDeliveryToD1(env.DB, {
        stripe_session_id: sessionId,
        customer_email: customerEmail || 'unknown',
        product_name: 'UNKNOWN',
        notion_url: null,
        amount_paid: amountPaid,
        currency,
        delivery_status: 'failed_unknown_product',
      });
      // Return 200 so Stripe does not retry — we still want to log and investigate
      return jsonResponse({
        received: true,
        action: 'logged_unknown_product',
        session_id: sessionId,
      }, 200);
    }

    // -----------------------------------------------------------------------
    // 5. Validate email
    // -----------------------------------------------------------------------
    if (!customerEmail) {
      console.error(`No customer email for session ${sessionId}`);
      await logDeliveryToD1(env.DB, {
        stripe_session_id: sessionId,
        customer_email: 'unknown',
        product_name: product.name,
        notion_url: product.notion_url,
        amount_paid: amountPaid,
        currency,
        delivery_status: 'failed_no_email',
      });
      return jsonResponse({ received: true, action: 'failed_no_email', session_id: sessionId }, 200);
    }

    // -----------------------------------------------------------------------
    // 6. Send delivery email via MailChannels
    // -----------------------------------------------------------------------
    let emailStatus = 'sent';
    try {
      await sendDeliveryEmail(customerEmail, product);
      console.log(`Delivery email sent to ${customerEmail} for ${product.name}`);
    } catch (emailErr) {
      console.error('Email delivery failed:', emailErr.message);
      emailStatus = 'email_failed';
      // Still log the transaction — admin can resend manually
    }

    // -----------------------------------------------------------------------
    // 7. Log to D1
    // -----------------------------------------------------------------------
    await logDeliveryToD1(env.DB, {
      stripe_session_id: sessionId,
      customer_email: customerEmail,
      product_name: product.name,
      notion_url: product.notion_url,
      amount_paid: amountPaid,
      currency,
      delivery_status: emailStatus,
    });

    return jsonResponse({
      received: true,
      action: 'delivered',
      product: product.name,
      email: customerEmail,
      email_status: emailStatus,
    }, 200);
  },
};

// ---------------------------------------------------------------------------
// Stripe HMAC-SHA256 signature verification (no external library needed)
// Implements: https://stripe.com/docs/webhooks/signatures
// ---------------------------------------------------------------------------
async function verifyStripeSignature(rawBody, signatureHeader, secret) {
  if (!secret) throw new Error('STRIPE_WEBHOOK_SECRET env var is not set');

  // Parse the signature header: t=timestamp,v1=hash[,v1=hash...]
  const parts = {};
  for (const part of signatureHeader.split(',')) {
    const [k, v] = part.split('=');
    if (k === 't') parts.timestamp = v;
    else if (k === 'v1') {
      if (!parts.signatures) parts.signatures = [];
      parts.signatures.push(v);
    }
  }

  if (!parts.timestamp || !parts.signatures?.length) {
    throw new Error('Malformed stripe-signature header');
  }

  // Reject webhooks older than 5 minutes (replay attack protection)
  const tolerance = 300; // seconds
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - parseInt(parts.timestamp, 10)) > tolerance) {
    throw new Error(`Webhook timestamp too old (${parts.timestamp})`);
  }

  // Compute HMAC
  const signedPayload = `${parts.timestamp}.${rawBody}`;
  const encoder = new TextEncoder();
  const keyData = encoder.encode(secret);
  const msgData = encoder.encode(signedPayload);

  const cryptoKey = await crypto.subtle.importKey(
    'raw',
    keyData,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const signatureBuffer = await crypto.subtle.sign('HMAC', cryptoKey, msgData);
  const expectedHex = bufferToHex(signatureBuffer);

  const isValid = parts.signatures.some((sig) => constantTimeEqual(sig, expectedHex));
  if (!isValid) throw new Error('Signature mismatch');

  return JSON.parse(rawBody);
}

function bufferToHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

// Timing-safe string comparison (prevents timing attacks)
function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}

// ---------------------------------------------------------------------------
// Product resolution helpers
// ---------------------------------------------------------------------------
function findProductByName(name) {
  const normalized = (name || '').toLowerCase().trim();
  for (const product of Object.values(PRODUCTS)) {
    if (product.name.toLowerCase().includes(normalized) || normalized.includes(product.name.toLowerCase())) {
      return product;
    }
  }
  return null;
}

function findProductByAmount(amount) {
  // Sort by specificity (exact match first, then nearest)
  for (const product of Object.values(PRODUCTS)) {
    if (product.price_amount === amount) return product;
  }
  return null;
}

async function resolveProductFromLineItems(sessionId, stripeSecretKey) {
  const url = `https://api.stripe.com/v1/checkout/sessions/${sessionId}/line_items?expand[]=data.price.product&limit=5`;
  const resp = await fetch(url, {
    headers: {
      Authorization: `Bearer ${stripeSecretKey}`,
    },
  });

  if (!resp.ok) {
    throw new Error(`Stripe API returned ${resp.status}`);
  }

  const data = await resp.json();
  const items = data.data || [];

  for (const item of items) {
    const productId = item.price?.product?.id || item.price?.product;
    if (productId && PRODUCT_BY_STRIPE_ID[productId]) {
      return PRODUCT_BY_STRIPE_ID[productId];
    }
    // Also try product name
    const productName = item.price?.product?.name || item.description;
    if (productName) {
      const match = findProductByName(productName);
      if (match) return match;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// MailChannels email delivery
// ---------------------------------------------------------------------------
async function sendDeliveryEmail(toEmail, product) {
  const subject = `Your ${product.name} is ready — here's your access link 🚀`;
  const htmlBody = buildEmailHtml(toEmail, product);
  const textBody = buildEmailText(toEmail, product);

  const payload = {
    personalizations: [
      {
        to: [{ email: toEmail }],
      },
    ],
    from: {
      email: FROM_EMAIL,
      name: FROM_NAME,
    },
    subject,
    content: [
      { type: 'text/plain', value: textBody },
      { type: 'text/html', value: htmlBody },
    ],
  };

  const resp = await fetch('https://api.mailchannels.net/tx/v1/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (resp.status !== 202 && resp.status !== 200) {
    const errBody = await resp.text();
    throw new Error(`MailChannels responded ${resp.status}: ${errBody}`);
  }
}

// ---------------------------------------------------------------------------
// HTML email template
// ---------------------------------------------------------------------------
function buildEmailHtml(toEmail, product) {
  const hasPdf = !!product.pdf_url;
  const pdfSection = hasPdf
    ? `
      <tr>
        <td style="padding: 0 40px 24px;">
          <p style="margin: 0; font-size: 15px; color: #6b7280; text-align: center;">
            Also included:
            <a href="${product.pdf_url}" style="color: #7c3aed; text-decoration: underline;">
              Download the Quick-Start PDF Guide
            </a>
          </p>
        </td>
      </tr>`
    : '';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Your ${product.name} is Ready</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f3f4f6; padding: 40px 20px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.08);">

          <!-- Header -->
          <tr>
            <td style="background: linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #4c1d95 100%); padding: 48px 40px 40px; text-align: center;">
              <p style="margin: 0 0 16px; font-size: 13px; font-weight: 600; letter-spacing: 3px; text-transform: uppercase; color: #a78bfa;">
                Purchase Confirmed
              </p>
              <h1 style="margin: 0; font-size: 30px; font-weight: 800; color: #ffffff; line-height: 1.2;">
                Your ${product.name}<br/>is ready to use! 🚀
              </h1>
              <p style="margin: 16px 0 0; font-size: 16px; color: #c4b5fd;">
                ${product.description}
              </p>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding: 40px 40px 24px;">
              <p style="margin: 0 0 8px; font-size: 17px; font-weight: 700; color: #1e1b4b;">
                Hey there 👋
              </p>
              <p style="margin: 0 0 24px; font-size: 15px; line-height: 1.7; color: #374151;">
                Thanks for your purchase! Your <strong>${product.name}</strong> template is ready and waiting for you in Notion. Click the button below to access it immediately.
              </p>
            </td>
          </tr>

          <!-- CTA Button(s) -->
          <tr>
            <td style="padding: 0 40px 32px; text-align: center;">
              <a href="${product.notion_url}"
                 style="display: inline-block; background: linear-gradient(135deg, #7c3aed, #4f46e5); color: #ffffff; font-size: 17px; font-weight: 700; text-decoration: none; padding: 18px 48px; border-radius: 12px; letter-spacing: 0.3px; box-shadow: 0 4px 14px rgba(124, 58, 237, 0.4);">
                ${product.notion_url_2 ? 'Access FreelanceOS Pro →' : 'Access Your Template →'}
              </a>
              ${product.notion_url_2 ? `
              <br/><br/>
              <a href="${product.notion_url_2}"
                 style="display: inline-block; background: linear-gradient(135deg, #0ea5e9, #0284c7); color: #ffffff; font-size: 17px; font-weight: 700; text-decoration: none; padding: 18px 48px; border-radius: 12px; letter-spacing: 0.3px; box-shadow: 0 4px 14px rgba(14, 165, 233, 0.4);">
                Access CreatorHQ Pro →
              </a>` : ''}
            </td>
          </tr>

          ${pdfSection}

          <!-- Divider -->
          <tr>
            <td style="padding: 0 40px;">
              <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 0;" />
            </td>
          </tr>

          <!-- Setup Steps -->
          <tr>
            <td style="padding: 32px 40px 24px;">
              <h2 style="margin: 0 0 20px; font-size: 18px; font-weight: 700; color: #1e1b4b;">
                Getting started in 3 steps
              </h2>

              <!-- Step 1 -->
              <table cellpadding="0" cellspacing="0" style="margin-bottom: 16px; width: 100%;">
                <tr>
                  <td width="40" valign="top">
                    <div style="width: 32px; height: 32px; border-radius: 50%; background: linear-gradient(135deg, #7c3aed, #4f46e5); color: white; font-size: 14px; font-weight: 700; line-height: 32px; text-align: center;">1</div>
                  </td>
                  <td valign="top" style="padding-top: 6px;">
                    <p style="margin: 0 0 3px; font-size: 15px; font-weight: 600; color: #111827;">Click "Access Your Template"</p>
                    <p style="margin: 0; font-size: 14px; color: #6b7280; line-height: 1.5;">The link above opens the Notion template in your browser.</p>
                  </td>
                </tr>
              </table>

              <!-- Step 2 -->
              <table cellpadding="0" cellspacing="0" style="margin-bottom: 16px; width: 100%;">
                <tr>
                  <td width="40" valign="top">
                    <div style="width: 32px; height: 32px; border-radius: 50%; background: linear-gradient(135deg, #7c3aed, #4f46e5); color: white; font-size: 14px; font-weight: 700; line-height: 32px; text-align: center;">2</div>
                  </td>
                  <td valign="top" style="padding-top: 6px;">
                    <p style="margin: 0 0 3px; font-size: 15px; font-weight: 600; color: #111827;">Duplicate to your workspace</p>
                    <p style="margin: 0; font-size: 14px; color: #6b7280; line-height: 1.5;">In Notion, click <strong>"Duplicate"</strong> (top-right corner) to add it to your own workspace. You need a free Notion account.</p>
                  </td>
                </tr>
              </table>

              <!-- Step 3 -->
              <table cellpadding="0" cellspacing="0" style="margin-bottom: 0; width: 100%;">
                <tr>
                  <td width="40" valign="top">
                    <div style="width: 32px; height: 32px; border-radius: 50%; background: linear-gradient(135deg, #7c3aed, #4f46e5); color: white; font-size: 14px; font-weight: 700; line-height: 32px; text-align: center;">3</div>
                  </td>
                  <td valign="top" style="padding-top: 6px;">
                    <p style="margin: 0 0 3px; font-size: 15px; font-weight: 600; color: #111827;">Customise and start using it</p>
                    <p style="margin: 0; font-size: 14px; color: #6b7280; line-height: 1.5;">Edit every page, database, and view to match your workflow. It's yours forever.</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="padding: 0 40px;">
              <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 0;" />
            </td>
          </tr>

          <!-- Need Help -->
          <tr>
            <td style="padding: 28px 40px;">
              <table cellpadding="0" cellspacing="0" style="background: #f9fafb; border-radius: 10px; width: 100%;">
                <tr>
                  <td style="padding: 20px 24px;">
                    <p style="margin: 0 0 6px; font-size: 15px; font-weight: 600; color: #111827;">
                      Need help? I've got you. 💬
                    </p>
                    <p style="margin: 0; font-size: 14px; color: #6b7280; line-height: 1.6;">
                      If you have any questions, trouble accessing your template, or just want to share how you're using it, reply to this email or reach out directly at
                      <a href="mailto:${SUPPORT_EMAIL}" style="color: #7c3aed; text-decoration: none; font-weight: 600;">${SUPPORT_EMAIL}</a>
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background: #f9fafb; padding: 24px 40px; text-align: center; border-top: 1px solid #e5e7eb;">
              <p style="margin: 0 0 6px; font-size: 13px; font-weight: 600; color: #4c1d95;">FreelanceOS</p>
              <p style="margin: 0; font-size: 12px; color: #9ca3af; line-height: 1.6;">
                You're receiving this because you purchased <strong>${product.name}</strong>.<br/>
                This is a one-time delivery email — keep it safe!
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>`;
}

function buildEmailText(toEmail, product) {
  const accessSection = product.notion_url_2
    ? `ACCESS YOUR TEMPLATES:\n\nFreelanceOS Pro:\n${product.notion_url}\n\nCreatorHQ Pro:\n${product.notion_url_2}`
    : `ACCESS YOUR TEMPLATE:\n${product.notion_url}`;

  return `Your ${product.name} is ready!

Hi there,

Thanks for your purchase. Your ${product.name} template is ready to use.

${accessSection}

GETTING STARTED IN 3 STEPS:

1. Click the link above — it opens your Notion template in the browser.
2. Click "Duplicate" (top-right corner in Notion) to add it to your workspace.
   You'll need a free Notion account at https://www.notion.so
3. Customise every page, database, and view to match your workflow.
   It's yours forever.

${product.pdf_url ? `QUICK-START GUIDE (PDF):
${product.pdf_url}

` : ''}Need help? Reach out at ${SUPPORT_EMAIL} — I respond to every email.

—
FreelanceOS
This is a one-time delivery email — keep it safe!
`;
}

// ---------------------------------------------------------------------------
// D1 logging helper
// ---------------------------------------------------------------------------
async function logDeliveryToD1(db, record) {
  if (!db) {
    console.warn('D1 binding (DB) not available — skipping database log');
    return;
  }
  try {
    await db
      .prepare(
        `INSERT INTO deliveries
          (stripe_session_id, customer_email, product_name, notion_url, amount_paid, currency, delivery_status)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
      .bind(
        record.stripe_session_id,
        record.customer_email,
        record.product_name,
        record.notion_url ?? null,
        record.amount_paid ?? null,
        record.currency ?? null,
        record.delivery_status ?? 'sent'
      )
      .run();
    console.log(`D1 log written for session ${record.stripe_session_id}`);
  } catch (err) {
    // UNIQUE constraint = duplicate insert — safe to ignore
    if (err.message?.includes('UNIQUE constraint')) {
      console.warn(`D1: duplicate insert for session ${record.stripe_session_id} — ignored`);
    } else {
      console.error('D1 insert failed:', err.message);
    }
  }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------
function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
