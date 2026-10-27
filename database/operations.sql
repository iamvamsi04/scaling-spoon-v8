-- Additive migration. Original orders and invoice lines remain intact.
CREATE TABLE IF NOT EXISTS order_details (
    order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled')),
    due_date TEXT,
    currency TEXT NOT NULL,
    customer_snapshot TEXT,
    cancellation_reason TEXT
);
CREATE TABLE IF NOT EXISTS billing_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON billing_audit
BEGIN SELECT RAISE(ABORT, 'Audit records cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON billing_audit
BEGIN SELECT RAISE(ABORT, 'Audit records cannot be deleted'); END;
CREATE INDEX IF NOT EXISTS ix_order_due ON order_details(due_date,status);

CREATE TABLE IF NOT EXISTS refund_requests (
    order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
    amount DECIMAL(10,2) NOT NULL CHECK(amount > 0),
    currency TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved')),
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    approved_by_role TEXT CHECK(approved_by_role IN ('cashier','admin')),
    CHECK((status='pending' AND approved_at IS NULL AND approved_by_role IS NULL)
       OR (status='approved' AND approved_at IS NOT NULL AND approved_by_role IS NOT NULL))
);
