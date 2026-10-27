-- Additive schema: existing product information, orders and financial records are untouched.
CREATE TABLE IF NOT EXISTS product_attributes (
    item_code TEXT NOT NULL REFERENCES items(item_code),
    attribute TEXT NOT NULL COLLATE NOCASE CHECK(length(trim(attribute)) BETWEEN 1 AND 100),
    text_value TEXT CHECK(text_value IS NULL OR length(text_value)<=500),
    number_value REAL CHECK(number_value IS NULL OR abs(number_value)<=1e15),
    unit TEXT CHECK(unit IS NULL OR length(unit)<=30),
    PRIMARY KEY(item_code,attribute),
    CHECK((text_value IS NOT NULL AND number_value IS NULL AND unit IS NULL)
       OR (text_value IS NULL AND number_value IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_attributes_numeric ON product_attributes(attribute,unit,number_value);
CREATE INDEX IF NOT EXISTS ix_attributes_text ON product_attributes(attribute,text_value);
CREATE INDEX IF NOT EXISTS ix_orders_date ON orders(order_date);
CREATE TABLE IF NOT EXISTS product_relations (
    item_code TEXT NOT NULL REFERENCES items(item_code),
    related_item_code TEXT NOT NULL REFERENCES items(item_code),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('substitute','compatible','accessory')),
    notes TEXT NOT NULL DEFAULT '' CHECK(length(notes)<=2000),
    PRIMARY KEY(item_code,related_item_code,relation_type),
    CHECK(item_code<>related_item_code)
);
-- Relations are directional; store the reverse explicitly when appropriate.
CREATE INDEX IF NOT EXISTS ix_relations_reverse ON product_relations(related_item_code,relation_type);
