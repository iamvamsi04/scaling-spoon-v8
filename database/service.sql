
CREATE TABLE IF NOT EXISTS customer_billing_details (
 customer_id TEXT PRIMARY KEY REFERENCES customers(customer_id), billing_address TEXT NOT NULL DEFAULT '',
 tax_registration_number TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS order_service (
 order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
 fulfillment_status TEXT COLLATE NOCASE NOT NULL DEFAULT 'unknown' CHECK(fulfillment_status COLLATE BINARY IN ('unknown','placed','shipped','delivered','cancelled')),
 shipped_at TEXT, delivered_at TEXT, cancelled_at TEXT, cancellation_restocked INTEGER NOT NULL DEFAULT 0,
 credit_requested DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK(credit_requested>=0),
 review_note TEXT, review_requested_at TEXT
);
CREATE TABLE IF NOT EXISTS invoice_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL REFERENCES orders(order_id),
 contact_json TEXT NOT NULL CHECK(json_valid(contact_json)), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS refund_details (
 order_id TEXT PRIMARY KEY REFERENCES refund_requests(order_id),
 items_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(items_json)),
 disposition TEXT NOT NULL DEFAULT 'original_method' CHECK(disposition IN ('original_method','store_credit')),
 target_order_id TEXT REFERENCES orders(order_id),
 request_kind TEXT NOT NULL DEFAULT 'return' CHECK(request_kind IN ('return','cancellation','billing_adjustment')),
 return_received INTEGER NOT NULL DEFAULT 0 CHECK(return_received IN (0,1)),
 waiver_reason TEXT, escalated_at TEXT, escalation_note TEXT
);
CREATE TABLE IF NOT EXISTS store_credit_ledger (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL REFERENCES customers(customer_id),
 order_id TEXT NOT NULL REFERENCES orders(order_id), currency TEXT NOT NULL,
 amount DECIMAL(10,2) NOT NULL CHECK(amount<>0),
 kind TEXT NOT NULL CHECK(kind IN ('refund_credit','order_redemption')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(order_id,kind),
 CHECK((kind='refund_credit' AND amount>0) OR (kind='order_redemption' AND amount<0))
);
CREATE TRIGGER IF NOT EXISTS credit_no_update BEFORE UPDATE ON store_credit_ledger
BEGIN SELECT RAISE(ABORT, 'Credit ledger is append only'); END;
CREATE TRIGGER IF NOT EXISTS credit_no_delete BEFORE DELETE ON store_credit_ledger
BEGIN SELECT RAISE(ABORT, 'Credit ledger is append only'); END;
CREATE INDEX IF NOT EXISTS ix_credit_customer_currency ON store_credit_ledger(customer_id,currency);
CREATE INDEX IF NOT EXISTS ix_invoice_revisions_order ON invoice_revisions(order_id,id);

CREATE TABLE IF NOT EXISTS customer_credentials (
 customer_id TEXT PRIMARY KEY REFERENCES customers(customer_id), password_hash TEXT NOT NULL,
 failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS customer_sessions (
 token_hash TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customer_credentials(customer_id),
 expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_customer_sessions_owner ON customer_sessions(customer_id);
