# FreelanceOS Storefront

## Purpose
The primary production website and digital product storefront for FreelanceOS. Serves 300+ SEO-optimized static HTML pages targeting freelancers, solopreneurs, and Notion template seekers. Sells Notion template products (FreelanceOS Pro $29, CreatorHQ Pro, AgencyOS, SoloFounderOS, bundles) via Stripe checkout. Deployed to Vercel.

## Tech Stack
- Static HTML/CSS/JS — no framework, no build step
- Vercel hosting with `vercel.json` config (cleanUrls, caching headers, redirects)
- Cloudflare Workers (webhook-worker) for Stripe webhook handling + email delivery
- Cloudflare D1 (SQLite) for transaction logging (webhook-worker/schema.sql)
- Stripe for payments (checkout links embedded in HTML)
- MailChannels for transactional email delivery (via Cloudflare Worker)
- Google Fonts (Inter)

## Architecture
Flat-file static site with hundreds of HTML pages. No templating engine — each page is hand-authored HTML. Pages are organized by category in the root directory. Sub-directories handle specific concerns:

- `checkout/` — Stripe checkout bridge pages (redirect to Stripe with promo codes pre-applied)
- `blog/` — Blog post HTML pages
- `downloads/` — Downloadable PDFs and Notion template ZIP files
- `webhook-worker/` — Cloudflare Worker source (Stripe webhook → D1 log → MailChannels delivery email)
- `vs/` — Product comparison pages
- `youtube-pipeline/` — YouTube content pipeline scripts (nested from OneDrive symlink)
- `OneDrive/` — Symlinked OneDrive content (not part of the static site)

## Development
No build step required. Edit HTML files directly.

**Preview locally:**
```
npx serve . --listen 3000
```

**Deploy to Vercel:**
```
vercel --prod
```

**Webhook Worker (Cloudflare):**
```
cd webhook-worker
wrangler deploy
wrangler secret put STRIPE_WEBHOOK_SECRET
wrangler secret put STRIPE_SECRET_KEY
```

## Project Structure
```
freelanceos-storefront/
├── index.html                   # Homepage (main product landing)
├── vercel.json                  # Vercel config: cleanUrls, caching, redirects
├── sitemap.xml                  # XML sitemap (672+ URLs)
├── robots.txt                   # Crawl directives
├── 404.html                     # Custom 404 page
├── og-image.png                 # Open Graph social share image
├── email-capture.js             # Email capture JS snippet
├── checkout/                    # Stripe checkout bridge pages
│   ├── freelanceos-pro.html
│   ├── creatorhq-pro.html
│   ├── agencyos-pro.html
│   ├── solofounderos-pro.html
│   └── bundle.html
├── blog/                        # Blog post pages
├── downloads/                   # FreelanceOS-Pro.zip, CreatorHQ-Pro.zip, PDFs
├── webhook-worker/              # Cloudflare Worker: Stripe webhooks + delivery email
│   ├── worker.js                # Main worker (webhook verify → D1 log → email)
│   ├── wrangler.toml            # Cloudflare config (D1 binding, secrets)
│   ├── schema.sql               # D1 database schema for transaction log
│   └── README.md
├── vs/                          # Comparison pages (notion-vs-*.html)
├── notion-*.html                # 200+ Notion template SEO landing pages
├── freelance-*.html             # Freelance tool/topic SEO pages
└── [300+ additional HTML pages]
```

## Environment Variables

| Variable | Description | Required |
|---|---|---|
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret | Yes (Worker) |
| `STRIPE_SECRET_KEY` | Stripe API secret key | Yes (Worker) |

All secrets set via `wrangler secret put`, never stored in files.

## Key Files
- `index.html` — main product landing page
- `vercel.json` — routing, caching, and redirect rules
- `webhook-worker/worker.js` — Stripe webhook handler (verify signature → identify product → send email → log to D1)
- `webhook-worker/wrangler.toml` — Cloudflare Worker config; D1 database_id is `TODO_REPLACE_WITH_D1_DATABASE_ID`
- `webhook-worker/schema.sql` — D1 deliveries table schema
- `sitemap.xml` — full sitemap (update with each SEO wave)

## Deployment
- **Static site:** `vercel --prod` from repo root. Vercel project connected to GitHub for auto-deploy on push.
- **Webhook Worker:** `wrangler deploy` from `webhook-worker/` directory. Worker name: `freelanceos-webhook`.
- Vercel redirects: `/freelanceos-pro` → `/checkout/freelanceos-pro`, `/creatorhq-pro` → `/checkout/creatorhq-pro`, `/agencyos-pro` → `/checkout/agencyos-pro`

## Known Issues / Gotchas
- `webhook-worker/wrangler.toml` has `database_id = "TODO_REPLACE_WITH_D1_DATABASE_ID"` — must be replaced with real D1 ID before worker deploys correctly.
- `worker.js` product catalog has `notion_url: 'TODO_REPLACE_WITH_REAL_NOTION_SHARE_URL'` for most products — these need real Notion share links.
- `email-capture-snippet.html` and `email-capture.js` are used across pages; changes must be manually replicated.
- The `youtube-pipeline/` and `OneDrive/` subdirectories in this folder are symlinks/nested repos, not part of the deployed site.
- SEO waves add 10-20 HTML pages per batch; sitemap.xml must be manually rebuilt after each wave.
- `checkout/` subdirectory shows empty in some PowerShell scans — pages live at root-level paths, Vercel redirects handle routing.
