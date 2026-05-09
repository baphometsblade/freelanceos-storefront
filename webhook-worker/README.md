# FreelanceOS Webhook Worker — Deployment Guide

Cloudflare Worker that handles Stripe `checkout.session.completed` events:
verifies the signature, identifies the product, sends a delivery email via
MailChannels, and logs the transaction to Cloudflare D1.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Node.js | 18+ | https://nodejs.org |
| Wrangler CLI | 3+ | `npm install -g wrangler` |
| Cloudflare account | Free tier works | https://dash.cloudflare.com |

---

## Step 1 — Authenticate Wrangler

```bash
wrangler login
```

This opens a browser to authenticate with your Cloudflare account.

---

## Step 2 — Create the D1 database

```bash
wrangler d1 create freelanceos-deliveries
```

Wrangler prints output like:

```
✅ Successfully created DB 'freelanceos-deliveries'
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Copy that `database_id` and paste it into `wrangler.toml`:

```toml
[[d1_databases]]
binding = "DB"
database_name = "freelanceos-deliveries"
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"   # <-- paste here
```

---

## Step 3 — Apply the D1 schema

```bash
# Local dev database
wrangler d1 execute freelanceos-deliveries --file=schema.sql

# Production database
wrangler d1 execute freelanceos-deliveries --file=schema.sql --remote
```

---

## Step 4 — Set secrets

Secrets are never stored in `wrangler.toml`. Set them via the CLI:

```bash
# Your Stripe webhook signing secret (starts with whsec_)
# Find it at: Stripe Dashboard → Developers → Webhooks → select endpoint → Signing secret
wrangler secret put STRIPE_WEBHOOK_SECRET

# Your Stripe secret key (starts with sk_live_ or sk_test_)
# Find it at: Stripe Dashboard → Developers → API keys
wrangler secret put STRIPE_SECRET_KEY
```

Wrangler will prompt you to paste each value — it is never echoed to the
terminal or stored in any file.

---

## Step 5 — Add your real Notion URLs

Open `worker.js` and replace every `TODO_REPLACE_WITH_REAL_NOTION_SHARE_URL`
with the actual published Notion share link for each template.

**How to get a Notion share link:**
1. Open the template page in Notion.
2. Click **Share** (top right).
3. Toggle **Share to web** ON.
4. Click **Copy link**.
5. Paste into `worker.js`.

Do this for all four products: FreelanceOS Pro, FreelanceOS Quick Start,
CreatorHQ Pro, and Ultimate Creator Bundle.

---

## Step 6 — Deploy the Worker

```bash
wrangler deploy
```

Wrangler prints the Worker URL, e.g.:

```
https://freelanceos-webhook.<your-subdomain>.workers.dev
```

Keep this URL — you need it in the next step.

---

## Step 7 — Configure the Stripe webhook

1. Go to **Stripe Dashboard → Developers → Webhooks**.
2. Click **Add endpoint**.
3. Set **Endpoint URL** to your Worker URL from Step 6.
4. Under **Events to listen to**, select:
   - `checkout.session.completed`
5. Click **Add endpoint**.
6. Click **Reveal** next to **Signing secret** and copy the `whsec_...` value.
7. Run `wrangler secret put STRIPE_WEBHOOK_SECRET` and paste that value.

---

## Step 8 — (Optional) Add MailChannels SPF record

MailChannels requires an SPF DNS record on your sending domain so emails don't
land in spam.

For `FROM_EMAIL = 'noreply@freelanceos.site'`, add a TXT record to your DNS:

| Name | Type | Value |
|------|------|-------|
| `freelanceos.site` | TXT | `v=spf1 include:relay.mailchannels.net ~all` |

Also add a `_mailchannels` TXT record to lock down which workers can send from
your domain (prevents spoofing):

| Name | Type | Value |
|------|------|-------|
| `_mailchannels.freelanceos.site` | TXT | `v=mc1 cfid=<your-workers-subdomain>.workers.dev` |

Replace `<your-workers-subdomain>` with the subdomain shown in your Cloudflare
Workers dashboard (e.g. `baphometsblade`).

---

## Local development / testing

```bash
# Start a local dev server
wrangler dev

# In a separate terminal, forward a real Stripe test event:
# (requires Stripe CLI: https://stripe.com/docs/stripe-cli)
stripe listen --forward-to http://localhost:8787
stripe trigger checkout.session.completed
```

---

## Querying the D1 database

```bash
# See all deliveries
wrangler d1 execute freelanceos-deliveries --remote \
  --command="SELECT * FROM deliveries ORDER BY delivered_at DESC LIMIT 50;"

# Failed deliveries that need attention
wrangler d1 execute freelanceos-deliveries --remote \
  --command="SELECT * FROM deliveries WHERE delivery_status != 'sent';"

# Revenue total
wrangler d1 execute freelanceos-deliveries --remote \
  --command="SELECT SUM(amount_paid)/100.0 AS total_usd FROM deliveries WHERE delivery_status='sent';"
```

---

## Environment variable reference

| Variable | Where to get it | How to set it |
|----------|----------------|---------------|
| `STRIPE_WEBHOOK_SECRET` | Stripe → Webhooks → your endpoint → Signing secret | `wrangler secret put STRIPE_WEBHOOK_SECRET` |
| `STRIPE_SECRET_KEY` | Stripe → API keys → Secret key | `wrangler secret put STRIPE_SECRET_KEY` |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| 400 "Invalid signature" | Wrong `STRIPE_WEBHOOK_SECRET` | Re-copy from Stripe dashboard and re-run `wrangler secret put` |
| Email not arriving | SPF not set up | Add MailChannels SPF record (Step 8) |
| "Unknown product" logged | Product ID not in `PRODUCTS` map | Add the `stripe_product_id` from the Stripe dashboard to `worker.js` |
| D1 insert error | Schema not applied | Run Step 3 again with `--remote` |
| Duplicate deliveries | Webhook retries after timeout | Already handled — idempotency check prevents double-send |

---

## Files

```
webhook-worker/
├── worker.js       Main Cloudflare Worker (ES module)
├── wrangler.toml   Wrangler configuration
├── schema.sql      D1 database schema + useful admin queries
└── README.md       This file
```
