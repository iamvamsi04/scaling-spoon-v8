PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS billing_config (
    key VARCHAR(50) PRIMARY KEY, value VARCHAR(100) NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    customer_id VARCHAR(50) PRIMARY KEY, name VARCHAR(200) COLLATE NOCASE NOT NULL,
    company VARCHAR(200), email VARCHAR(200)
);
CREATE TABLE IF NOT EXISTS items (
    sku VARCHAR(50) PRIMARY KEY, item_code VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(200) COLLATE NOCASE NOT NULL,
    baseprice DECIMAL(10,2) NOT NULL CHECK(baseprice >= 0),
    sellingprice DECIMAL(10,2) NOT NULL CHECK(sellingprice >= 0),
    availablequantity INTEGER NOT NULL DEFAULT 0 CHECK(availablequantity >= 0)
);
CREATE TABLE IF NOT EXISTS promotions (
    promo_code VARCHAR(50) PRIMARY KEY, description VARCHAR(300),
    discount_type VARCHAR(20) NOT NULL CHECK(discount_type IN ('percent','flat')),
    discount_value DECIMAL(10,2) NOT NULL CHECK(discount_value >= 0),
    expires_on DATE NOT NULL CHECK(length(expires_on)=10 AND expires_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(expires_on,'+0 days') IS NOT NULL AND date(expires_on,'+0 days')=expires_on),
    CHECK(discount_type != 'percent' OR discount_value <= 100)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id VARCHAR(50) PRIMARY KEY,
    order_date DATE NOT NULL DEFAULT CURRENT_DATE,
    customer_id VARCHAR(50) NOT NULL REFERENCES customers(customer_id),
    order_invoice VARCHAR(100),
    order_status TEXT COLLATE NOCASE NOT NULL DEFAULT 'draft' CHECK(order_status COLLATE BINARY IN ('draft','placed','cancelled')),
    subtotal DECIMAL(10,2) NOT NULL CHECK(subtotal >= 0),
    tax_amount DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK(tax_amount >= 0),
    shipping_amount DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK(shipping_amount >= 0),
    discount_applied DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK(discount_applied >= 0),
    charged_price DECIMAL(10,2) NOT NULL CHECK(charged_price >= 0),
    promo_code VARCHAR(50) REFERENCES promotions(promo_code),
    refund_amount DECIMAL(10,2), refund_status VARCHAR(20) COLLATE NOCASE, refund_reason TEXT,
    CHECK(refund_amount IS NULL OR (refund_amount > 0 AND refund_amount <= charged_price))
);
CREATE TABLE IF NOT EXISTS order_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id VARCHAR(50) NOT NULL REFERENCES orders(order_id),
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    quantity INTEGER NOT NULL CHECK(quantity > 0), UNIQUE(order_id,item_code)
);
CREATE TABLE IF NOT EXISTS order_fulfilled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id VARCHAR(50) NOT NULL REFERENCES orders(order_id),
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    quantity INTEGER NOT NULL CHECK(quantity > 0), UNIQUE(order_id,item_code)
);
CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id VARCHAR(50) NOT NULL REFERENCES orders(order_id),
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    qty_billed INTEGER NOT NULL CHECK(qty_billed > 0),
    unit_price_billed DECIMAL(10,2) NOT NULL CHECK(unit_price_billed >= 0),
    line_total DECIMAL(10,2) NOT NULL CHECK(line_total >= 0), UNIQUE(order_id,item_code)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    unit_price DECIMAL(10,2) NOT NULL CHECK(unit_price >= 0),
    effective_from DATE NOT NULL, effective_to DATE,
    CHECK(effective_to IS NULL OR effective_to >= effective_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_invoice_number ON orders(order_invoice)
    WHERE order_invoice IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_customer_orders ON orders(customer_id,order_date);
CREATE INDEX IF NOT EXISTS ix_price_dates ON price_history(item_code,effective_from,effective_to);

CREATE TABLE IF NOT EXISTS product_details (
    item_code TEXT PRIMARY KEY REFERENCES items(item_code),
    category TEXT COLLATE NOCASE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    features_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(features_json) AND json_type(features_json)='object')
);
CREATE INDEX IF NOT EXISTS ix_product_category ON product_details(category);
