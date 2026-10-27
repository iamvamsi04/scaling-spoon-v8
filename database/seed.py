"""Explicit, repeatable demo seed. Never runs automatically on application startup."""
from database.connection import initialize, transaction


def seed():
    initialize()
    with transaction() as conn:
        conn.executemany("INSERT OR IGNORE INTO customers VALUES (?,?,?,?)", [
            ("CUST001", "Alex Morgan", "Northwind Studio", "alex@example.com"),
            ("CUST002", "Jamie Chen", "Contoso Labs", "jamie@example.com"),
            ("CUST003", "Sam Rivera", None, "sam@example.com"),
            ("CUST004", "Taylor Reed", "Fabrikam", "taylor@example.com")])
        conn.executemany("INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?)", [
            ("SKU-KB", "ITEM001", "Mechanical keyboard", "45", "79.90", 40),
            ("SKU-MS", "ITEM002", "Wireless mouse", "15", "29.50", 60),
            ("SKU-MN", "ITEM003", "27 inch monitor", "150", "249.00", 15)])
        conn.executemany("INSERT OR IGNORE INTO promotions(promo_code,description,discount_type,discount_value,expires_on) VALUES (?,?,?,?,date('now','+365 days'))", [
            ("SAVE10", "Ten percent off merchandise", "percent", "10"),
            ("SAVE500", "500 off merchandise, capped at subtotal", "flat", "500")])
    print("Demo customers, items and promotions are ready. Existing records were preserved.")


if __name__ == "__main__":
    seed()
