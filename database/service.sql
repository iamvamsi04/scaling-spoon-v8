
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

CREATE TABLE IF NOT EXISTS customer_addresses (
 address_id TEXT PRIMARY KEY,
 customer_id TEXT NOT NULL REFERENCES customers(customer_id),
 label TEXT COLLATE NOCASE NOT NULL DEFAULT '',
 full_name TEXT COLLATE NOCASE NOT NULL CHECK(length(trim(full_name)) BETWEEN 1 AND 200),
 phone TEXT NOT NULL CHECK(length(phone) BETWEEN 7 AND 30),
 address_line1 TEXT NOT NULL CHECK(length(trim(address_line1)) BETWEEN 1 AND 200),
 address_line2 TEXT NOT NULL DEFAULT '',
 city TEXT COLLATE NOCASE NOT NULL CHECK(length(trim(city)) BETWEEN 1 AND 100),
 state TEXT COLLATE NOCASE NOT NULL CHECK(length(trim(state)) BETWEEN 1 AND 100),
 postal_code TEXT NOT NULL CHECK(length(trim(postal_code)) BETWEEN 1 AND 20),
 country TEXT NOT NULL CHECK(length(country)=2 AND country GLOB '[A-Z][A-Z]'),
 landmark TEXT NOT NULL DEFAULT '',
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 UNIQUE(address_id,customer_id), CHECK(active=1 OR is_default=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_customer_default_address ON customer_addresses(customer_id)
 WHERE active=1 AND is_default=1;
CREATE UNIQUE INDEX IF NOT EXISTS ix_order_delivery_owner ON orders(order_id,customer_id);
CREATE TABLE IF NOT EXISTS order_delivery (
 order_id TEXT PRIMARY KEY,
 customer_id TEXT NOT NULL,
 address_id TEXT NOT NULL,
 address_snapshot TEXT NOT NULL CHECK(json_valid(address_snapshot) AND json_type(address_snapshot)='object'),
 FOREIGN KEY(order_id,customer_id) REFERENCES orders(order_id,customer_id),
 FOREIGN KEY(address_id,customer_id) REFERENCES customer_addresses(address_id,customer_id)
);
CREATE INDEX IF NOT EXISTS ix_delivery_address ON order_delivery(address_id,customer_id);
CREATE TRIGGER IF NOT EXISTS delivery_no_issued_update BEFORE UPDATE ON order_delivery
 WHEN EXISTS(SELECT 1 FROM orders WHERE order_id=OLD.order_id AND order_status<>'draft')
BEGIN SELECT RAISE(ABORT, 'Issued delivery addresses are immutable'); END;
CREATE TRIGGER IF NOT EXISTS delivery_no_issued_delete BEFORE DELETE ON order_delivery
 WHEN EXISTS(SELECT 1 FROM orders WHERE order_id=OLD.order_id AND order_status<>'draft')
BEGIN SELECT RAISE(ABORT, 'Issued delivery addresses are immutable'); END;
