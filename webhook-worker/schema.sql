-- FreelanceOS Webhook Worker — D1 Schema
-- Run against your D1 database:
--   wrangler d1 execute freelanceos-deliveries --file=schema.sql
--   (add --remote to target production, omit for local dev)

CREATE TABLE IF NOT EXISTS deliveries (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  stripe_session_id  TEXT    UNIQUE NOT NULL,
  customer_email     TEXT    NOT NULL,
  product_name       TEXT    NOT NULL,
  notion_url         TEXT,
  amount_paid        INTEGER,            -- in cents (e.g. 2900 = $29.00)
  currency           TEXT,               -- lowercase ISO code, e.g. "usd"
  delivered_at       TEXT    DEFAULT (datetime('now')),
  delivery_status    TEXT    DEFAULT 'sent'
    -- possible values:
    --   sent                    email delivered successfully
    --   email_failed            email send attempt failed (Notion URL still logged)
    --   failed_unknown_product  could not match a product — needs manual review
    --   failed_no_email         Stripe session had no customer email
);

-- Index for fast look-up by email (useful for support queries)
CREATE INDEX IF NOT EXISTS idx_deliveries_email
  ON deliveries (customer_email);

-- Index for filtering by status (useful for retry queries)
CREATE INDEX IF NOT EXISTS idx_deliveries_status
  ON deliveries (delivery_status);

-- Useful admin queries
-- -----------------------------------------------
-- All successful deliveries:
--   SELECT * FROM deliveries WHERE delivery_status = 'sent' ORDER BY delivered_at DESC;
--
-- Failed / needs attention:
--   SELECT * FROM deliveries WHERE delivery_status != 'sent' ORDER BY delivered_at DESC;
--
-- Revenue total:
--   SELECT SUM(amount_paid) / 100.0 AS total_usd FROM deliveries WHERE delivery_status = 'sent';
--
-- Deliveries today:
--   SELECT * FROM deliveries WHERE date(delivered_at) = date('now');
