"""Create a complete billing dataset without Azure.

Run: python generate_seed.py
Options: --db database/store.db --seed 42 --as-of 2026-10-08
Relative database paths resolve against the project folder. The default is BILLING_DB.
Existing active records/config are preserved. Retired payment/credit records are archived before removal.
Refuse batches exceeding 500 main-table rows.
Upgrades existing datasets with product details and service scenarios.
Seed markers make reruns repeatable without duplicate inserts.
Each generated customer initially uses <customer_id>-Store!2026 to sign in.
Only salted password hashes are saved; existing passwords are preserved.
Billing records are validated and committed first. Password setup runs in
parallel batches and resumes missing accounts after an interruption.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
from time import perf_counter

from config import settings
from database.service_seed import seed_layout
from mcp_server.tools.billing_tools import money, price_for, totals, update_totals

MAIN_COUNTS = {"customers": 400, "items": 400, "orders": 350,
               "order_requests": 450, "order_fulfilled": 400, "invoice_lines": 400}
TABLES = (*MAIN_COUNTS, "price_history", "promotions", "billing_config", "order_details",
          "product_details", "product_attributes", "product_relations", "billing_audit", "customer_credentials")
MARKER = "billing_seed_v1"
ENRICHMENT_MARKER = 'billing_seed_v2'
PASSWORD_BATCH_SIZE = 25
PASSWORD_WORKERS = min(4, os.cpu_count() or 1)
FIRST = "Alex Jamie Sam Taylor Morgan Jordan Casey Avery Riley Cameron Quinn Devon".split()
LAST = "Morgan Chen Rivera Reed Patel Smith Lewis Brooks Park Carter Diaz Evans".split()
PRODUCTS = [
    ("Mechanical keyboard", 7990), ("Wireless mouse", 2950), ("27 inch monitor", 24900),
    ("USB-C hub", 4495), ("Laptop stand", 3990), ("HD webcam", 6490),
    ("Noise cancelling headset", 12900), ("External SSD", 10990),
    ("Desk lamp", 3490), ("Office chair", 18900), ("Ethernet adapter", 1990),
    ("Bluetooth speaker", 5990), ("Power bank", 4590), ("Monitor arm", 6990),
    ("USB cable", 1290), ("Laptop sleeve", 2490), ("Desk mat", 1790),
    ("Wi-Fi router", 8990), ("Portable projector", 34900), ("Document scanner", 22900),
]
CATEGORIES = ['keyboards','mice','monitors','hubs','stands','webcams','headsets','storage',
              'lamps','chairs','adapters','speakers','power_banks','monitor_arms','cables',
              'sleeves','desk_mats','routers','projectors','scanners']


def product_facts(family, variant):
    """Generate product specifications for each model."""
    connection = 'wireless' if variant % 2 else 'wired'
    definitions = [
        ({'connection':connection,'layout':'compact' if variant%3==0 else 'full size','compatibility':'Windows and macOS'},'battery_life',20+variant*2,'hours'),
        ({'connection':connection,'handedness':'ambidextrous'},'sensitivity',800*(1+variant%4),'dpi'),
        ({'resolution':['1080p','1440p','4K'][variant%3],'input':'HDMI'},'screen_size',[24,27,32][variant%3],'inch'),
        ({'host_port':'USB-C','display_output':'HDMI'},'port_count',4+variant%5,'ports'),
        ({'material':'aluminium','adjustable':True},'load_capacity',3+variant%4,'kg'),
        ({'connection':'USB','resolution':['1080p','1440p','4K'][variant%3]},'frame_rate',30 if variant%2 else 60,'fps'),
        ({'connection':connection,'noise_cancelling':bool(variant%2)},'battery_life',12+variant*2,'hours'),
        ({'interface':'USB-C','drive_type':'SSD'},'capacity',[256,512,1024,2048][variant%4],'GB'),
        ({'dimmable':True,'power_source':'USB'},'brightness',400+variant*25,'lumen'),
        ({'material':'mesh','adjustable_height':True},'load_capacity',100+variant*5,'kg'),
        ({'host_port':'USB-C','network_port':'Ethernet'},'network_speed',1000 if variant%2 else 2500,'Mbps'),
        ({'connection':'Bluetooth','water_resistant':bool(variant%2)},'battery_life',8+variant,'hours'),
        ({'output_port':'USB-C','fast_charging':True},'capacity',10000+variant*1000,'mAh'),
        ({'mount':'VESA 100x100','adjustable':True},'load_capacity',6+variant%5,'kg'),
        ({'connector_a':'USB-C','connector_b':'USB-C'},'length',1+variant%3,'m'),
        ({'material':'padded fabric','water_resistant':True},'screen_size',[13,14,15,16][variant%4],'inch'),
        ({'material':'fabric','washable':True},'width',60+variant*2,'cm'),
        ({'standard':'Wi-Fi 6','dual_band':True},'network_speed',1200+variant*100,'Mbps'),
        ({'input':'HDMI','resolution':'1080p'},'brightness',1500+variant*100,'lumen'),
        ({'connection':'USB','duplex':bool(variant%2)},'scan_speed',15+variant,'ppm'),
    ]
    return definitions[family]


def seed_product_details(conn):
    prefix, shopping, _, _ = seed_layout(conn)
    groups = {}
    for row in conn.execute(f"SELECT item_code FROM items WHERE item_code GLOB '{prefix}-ITEM-[0-9][0-9][0-9][0-9]' ORDER BY item_code").fetchall():
        code = row['item_code']
        index = int(code.rsplit('-',1)[1])-1
        family, variant = index%20, index//20
        features, attribute, number, unit = product_facts(family,variant)
        # Wired equipment has no battery; omit an inapplicable numeric attribute.
        if family in (0,6) and features['connection']=='wired':
            attribute,number,unit = 'cable_length',1.5+variant%3/2,'m'
        category = CATEGORIES[family]
        conn.execute('INSERT OR IGNORE INTO product_details VALUES (?,?,?,?)',
            (code,category,f'{category.replace("_", " ").capitalize()} with model-specific features.',json.dumps(features)))
        conn.execute('INSERT OR IGNORE INTO product_attributes VALUES (?,?,NULL,?,?)',(code,attribute,number,unit))
        actual_category = conn.execute('SELECT category FROM product_details WHERE item_code=?',(code,)).fetchone()[0]
        groups.setdefault(actual_category,[]).append(code)
    for codes in groups.values():
        if len(codes)>1:
            for index,code in enumerate(codes):
                conn.execute('INSERT OR IGNORE INTO product_relations VALUES (?,?,?,?)',
                    (code,codes[(index+1)%len(codes)],'substitute','Same-category alternative; compare required features before choosing.'))
    for category,related,kind in [('keyboards','mice','accessory'),('storage','hubs','compatible'),
                                   ('monitors','monitor_arms','accessory'),('hubs','cables','compatible')]:
        for code,target in zip(groups.get(category,[]),groups.get(related,[])):
            conn.execute('INSERT OR IGNORE INTO product_relations VALUES (?,?,?,?)',
                         (code,target,kind,'Recorded product relationship; applies to these models only.'))


def create_scenario_order(conn, oid, customer, lines, as_of, placed=False):
    if conn.execute('SELECT 1 FROM orders WHERE order_id=?',(oid,)).fetchone():
        return
    additions = {'orders':1,'order_requests':len(lines),'order_fulfilled':len(lines) if placed else 0,
                 'invoice_lines':len(lines) if placed else 0}
    for table,addition in additions.items():
        require(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]+addition<=500,
                f'Service scenarios would exceed 500 {table} rows; use a fresh --db path.')
    conn.execute('INSERT INTO orders(order_id,order_date,customer_id,subtotal,charged_price) VALUES (?,?,?,0,0)',
                 (oid,as_of.isoformat(),customer))
    subtotal = Decimal(0)
    for code,qty in lines:
        item = conn.execute('SELECT * FROM items WHERE item_code=?',(code,)).fetchone()
        require(item is not None and item['availablequantity']>=qty,f'{code} needs stock for the service scenario.')
        price = price_for(conn,item,as_of.isoformat())
        amount = money(price*qty)
        subtotal += amount
        conn.execute('INSERT INTO order_requests(order_id,item_code,quantity) VALUES (?,?,?)',(oid,code,qty))
        if placed:
            conn.execute('INSERT INTO order_fulfilled(order_id,item_code,quantity) VALUES (?,?,?)',(oid,code,qty))
            conn.execute('INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) VALUES (?,?,?,?,?)',
                         (oid,code,qty,str(price),str(amount)))
            updated = conn.execute('UPDATE items SET availablequantity=availablequantity-? WHERE item_code=? AND availablequantity>=?',(qty,code,qty))
            require(updated.rowcount==1,'Bulk order has insufficient inventory.')
    update_totals(conn,oid,totals(conn,subtotal))
    if placed:
        conn.execute("UPDATE orders SET order_invoice=?,order_status='placed' WHERE order_id=?",('INV-'+oid,oid))


def enrich(conn, as_of):
    prefix, shopping, _, _ = seed_layout(conn)
    seed_product_details(conn)
    customer = f'{prefix}-CUST-0001'
    require(conn.execute('SELECT 1 FROM customers WHERE customer_id=?',(customer,)).fetchone(),
            'The original customer batch is incomplete; use a fresh --db path.')
    create_scenario_order(conn,f'{shopping}-DRAFT',customer,[(f'{prefix}-ITEM-0001',1),(f'{prefix}-ITEM-0002',1)],as_of)
    if not conn.execute(f"SELECT 1 FROM orders WHERE order_id='{shopping}-BULK'").fetchone():
        for item in conn.execute(f"SELECT * FROM items WHERE item_code GLOB '{prefix}-ITEM-*' ORDER BY sellingprice DESC,item_code").fetchall():
            price = price_for(conn,item,as_of.isoformat())
            if price<=0:
                continue
            quantity = int((Decimal('6000')/price).to_integral_value(rounding=ROUND_CEILING))
            if quantity<=item['availablequantity']:
                create_scenario_order(conn,f'{shopping}-BULK',customer,[(item['item_code'],quantity)],as_of,placed=True)
                break
        else:
            raise ValueError('Not enough stock for a valid bulk invoice. Use a fresh --db path.')
    currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()[0]
    for order in conn.execute(f"SELECT * FROM orders WHERE order_id GLOB '{prefix}-*' ORDER BY order_id").fetchall():
        placed = order['order_status'] == 'placed'
        contact = dict(conn.execute('SELECT * FROM customers WHERE customer_id=?',(order['customer_id'],)).fetchone())
        conn.execute('''INSERT OR IGNORE INTO order_details(order_id,currency,due_date,customer_snapshot)
            VALUES (?,?,?,?)''',(order['order_id'],currency,
                (date.fromisoformat(order['order_date'])+timedelta(days=30)).isoformat() if placed else None,
                json.dumps(contact) if placed else None))
    from database.service_seed import seed_service_scenarios
    seed_service_scenarios(conn,as_of,create_scenario_order)
    conn.execute('INSERT INTO billing_audit(action,details_json) VALUES (?,?)',('seed_enrichment',
        json.dumps({'generated':True,'as_of':as_of.isoformat(),'note':'Generated fixture records, not live customer transactions.'})))


def audit_enrichment(conn):
    prefix, shopping, _, _ = seed_layout(conn)
    require(not conn.execute('PRAGMA foreign_key_check').fetchall(),'Foreign key violations found.')
    require(conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok','SQLite integrity check failed.')
    for table in MAIN_COUNTS:
        require(300<=conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]<=500,f'{table} is outside 300-500 rows.')
    for order in conn.execute(f"SELECT * FROM orders WHERE order_id GLOB '{prefix}-*'").fetchall():
        oid = order['order_id']
        require(money(order['subtotal'])-money(order['discount_applied'])+money(order['tax_amount'])+money(order['shipping_amount'])
                == money(order['charged_price']),f'{oid} total mismatch.')
        if order['order_status'] in ('placed', 'cancelled') and order['order_invoice']:
            requested = dict(conn.execute('SELECT item_code,quantity FROM order_requests WHERE order_id=?',(oid,)))
            fulfilled = dict(conn.execute('SELECT item_code,quantity FROM order_fulfilled WHERE order_id=?',(oid,)))
            lines = conn.execute('SELECT * FROM invoice_lines WHERE order_id=?',(oid,)).fetchall()
            require(fulfilled=={r['item_code']:r['qty_billed'] for r in lines} and all(requested.get(code,0)>=qty for code,qty in fulfilled.items()) and (requested==fulfilled or oid==f'{prefix}-SVC-PARTIAL'),f'{oid} quantity mismatch.')
            require(sum((money(r['line_total']) for r in lines),Decimal(0))==money(order['subtotal']),f'{oid} subtotal mismatch.')
            require(all(money(r['unit_price_billed'])*r['qty_billed']==money(r['line_total']) for r in lines),f'{oid} line arithmetic mismatch.')
        require(money(order['refund_amount'] or 0)<=money(order['charged_price']),f'{oid} refund exceeds invoice.')



def scenarios(conn):
    prefix, shopping, _, _ = seed_layout(conn)
    keyboards = conn.execute(f"SELECT item_code FROM product_details WHERE category='keyboards' AND item_code GLOB '{prefix}-*' ORDER BY item_code LIMIT 2").fetchall()
    expensive = conn.execute(f"SELECT i.item_code FROM items i JOIN product_details d ON d.item_code=i.item_code         WHERE d.category='keyboards' AND i.item_code GLOB '{prefix}-*' AND i.availablequantity>0         AND (SELECT COUNT(*) FROM price_history h WHERE h.item_code=i.item_code AND h.effective_from<=CURRENT_DATE AND (h.effective_to IS NULL OR h.effective_to>=CURRENT_DATE))<=1         ORDER BY COALESCE((SELECT unit_price FROM price_history h WHERE h.item_code=i.item_code AND h.effective_from<=CURRENT_DATE AND (h.effective_to IS NULL OR h.effective_to>=CURRENT_DATE)),i.sellingprice) DESC,i.item_code LIMIT 1").fetchone()
    return {'customer_id':f'{prefix}-CUST-0001','draft_order_id':f'{shopping}-DRAFT','refund_order_id':f'{shopping}-BULK',
            'invoice_number':'INV-'+shopping+'-BULK','comparison_items':[r[0] for r in keyboards],
            'cheaper_alternatives_source':expensive[0] if expensive else None,
            'promo_code':f'{prefix}-PERCENT-10','new_customer_example':'CUSTOMER-NEW-001',
            'service_examples':{'partial_invoice':f'{prefix}-SVC-PARTIAL','reported_duplicate_charge':f'{prefix}-SVC-DUPLICATE','b2g1_return':f'{prefix}-SVC-B2G','shipped':f'{prefix}-SVC-SHIPPED','delivered':f'{prefix}-SVC-DELIVERED','pending_above_threshold':f'{prefix}-SVC-PENDING'},
            'note':'Use the printed IDs with the existing tools. Order changes require customer confirmation; reruns do not reset consumed drafts or refunds.'}


def counts(conn):
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(conn, opening_stock):
    """Check references, prices, totals, refunds and inventory before committing."""
    prefix, shopping, _, _ = seed_layout(conn)
    require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "Foreign key violations found.")
    require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity check failed.")
    for table, count in counts(conn).items():
        if table in MAIN_COUNTS:
            require(300 <= count <= 500, f"{table} has {count} rows outside 300-500.")
    for order in conn.execute(f"SELECT * FROM orders WHERE order_id GLOB '{prefix}-ORD-*'").fetchall():
        oid = order["order_id"]
        requested = dict(conn.execute("SELECT item_code,quantity FROM order_requests WHERE order_id=?", (oid,)))
        fulfilled = dict(conn.execute("SELECT item_code,quantity FROM order_fulfilled WHERE order_id=?", (oid,)))
        invoice = conn.execute("SELECT * FROM invoice_lines WHERE order_id=?", (oid,)).fetchall()
        require(bool(requested), f"{oid} has no requested items.")
        subtotal = Decimal(0)
        if order['order_status'] in ('placed', 'cancelled') and order["order_invoice"]:
            require(requested == fulfilled, f"{oid} request and fulfillment mismatch.")
            require({r["item_code"]: r["qty_billed"] for r in invoice} == requested,
                    f"{oid} billed quantities do not match fulfillment.")
            for line in invoice:
                item = conn.execute("SELECT * FROM items WHERE item_code=?", (line["item_code"],)).fetchone()
                price = price_for(conn, item, order["order_date"])
                require(money(line["unit_price_billed"]) == price, f"{oid} incorrect historical price.")
                require(money(line["line_total"]) == money(price * line["qty_billed"]), f"{oid} incorrect line total.")
                subtotal += money(line["line_total"])
        else:
            require(not invoice and not fulfilled, f"{oid} draft has billed or fulfilled lines.")
            for code, quantity in requested.items():
                item = conn.execute("SELECT * FROM items WHERE item_code=?", (code,)).fetchone()
                require(item["availablequantity"] >= quantity, f"{oid} draft quantity exceeds stock.")
                subtotal += price_for(conn, item, order["order_date"]) * quantity
        for field, amount in totals(conn, subtotal, order["promo_code"], check_expiry=False).items():
            require(money(order[field]) == money(amount), f"{oid}: {field} is inconsistent.")
        if order["refund_amount"] is not None:
            require(bool(order["order_invoice"]) and order["refund_status"] == "recorded"
                    and bool(order["refund_reason"]), f"{oid} invalid refund metadata.")
            require(0 < money(order["refund_amount"]) <= money(order["charged_price"]), f"{oid} invalid refund amount.")
        else:
            require(order["refund_status"] is None and order["refund_reason"] is None, f"{oid} incomplete refund fields.")
    for code, initial in opening_stock.items():
        sold = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM order_fulfilled WHERE item_code=?", (code,)).fetchone()[0]
        remaining = conn.execute("SELECT availablequantity FROM items WHERE item_code=?", (code,)).fetchone()[0]
        require(remaining == initial - sold and remaining >= 0, f"{code} inventory mismatch.")
        history = conn.execute("SELECT * FROM price_history WHERE item_code=? ORDER BY effective_from", (code,)).fetchall()
        for old, new in zip(history, history[1:]):
            require(old["effective_to"] is not None and old["effective_to"] < new["effective_from"],
                    f"{code} overlapping price history.")


def seed_customer_passwords(conn, workers=PASSWORD_WORKERS, progress=None):
    """Save initial password hashes only for generated accounts without credentials."""
    from api.customer_auth import password_hash
    prefix = seed_layout(conn)[0]
    missing = conn.execute(
        'SELECT c.customer_id FROM customers c LEFT JOIN customer_credentials a '
        'ON a.customer_id=c.customer_id WHERE a.customer_id IS NULL '
        'AND c.customer_id GLOB ? ORDER BY c.customer_id',
        (prefix + '-CUST-[0-9][0-9][0-9][0-9]',)).fetchall()
    if not missing:
        if progress:
            progress('All generated customers already have passwords.')
        return 0
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 4:
        raise ValueError('Password workers must be between 1 and 4.')
    if progress:
        progress(f'Preparing {len(missing)} customer passwords with {workers} workers.')
    added = 0
    last_update = perf_counter()
    # SQLite stays on this thread. Workers only calculate hashes; no write lock
    # is held during the expensive calculations.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(missing), PASSWORD_BATCH_SIZE):
            batch = missing[start:start + PASSWORD_BATCH_SIZE]
            pending = {pool.submit(password_hash, row[0] + '-Store!2026'): row[0] for row in batch}
            prepared = []
            try:
                for job in as_completed(pending):
                    prepared.append((pending[job], job.result()))
                    if progress and perf_counter() - last_update >= 2:
                        progress(f'Password hashes prepared: {start + len(prepared)}/{len(missing)}.')
                        last_update = perf_counter()
            except BaseException:
                for job in pending:
                    job.cancel()
                raise
            try:
                conn.execute('BEGIN IMMEDIATE')
                for customer_id, encoded in sorted(prepared):
                    # A password set by another process while hashing always wins.
                    inserted = conn.execute(
                        'INSERT OR IGNORE INTO customer_credentials(customer_id,password_hash) '
                        'SELECT customer_id,? FROM customers WHERE customer_id=?', (encoded, customer_id))
                    added += inserted.rowcount
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            if progress:
                progress(f'Password accounts checked: {start + len(batch)}/{len(missing)}; {added} added.')
                last_update = perf_counter()
    return added


def generate_records(db_path: Path, seed: int, as_of: date):
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        from database.migrations import remove_retired_tables, remove_views
        remove_views(conn)
        backup = remove_retired_tables(conn)
        # Seed schema and records share a rollback boundary. Retired-table cleanup
        # is a separate migration; any populated retired records are backed up first.
        schema = '\n'.join((settings.ROOT / 'database' / name).read_text(encoding='utf-8')
                           for name in ('schema.sql','operations.sql','insights.sql','service.sql'))
        conn.executescript("BEGIN IMMEDIATE;\n" + schema)
        from database.migrations import add_promotion_expiry, add_unknown_fulfillment, add_order_status
        add_promotion_expiry(conn)
        add_unknown_fulfillment(conn)
        add_order_status(conn)
        prefix, shopping, marker, enrichment_marker = seed_layout(conn)
        if conn.execute("SELECT 1 FROM billing_config WHERE key=?", (enrichment_marker,)).fetchone():
            from database.service_seed import seed_service_scenarios
            seed_service_scenarios(conn,as_of,create_scenario_order)
            result = {"status": "already_seeded", "database": str(db_path), "rows": counts(conn), 'scenarios':scenarios(conn), 'retired_records_backup':backup}
            conn.commit()
            return result
        if conn.execute("SELECT 1 FROM billing_config WHERE key=?", (marker,)).fetchone():
            before = counts(conn)
            enrich(conn,as_of)
            audit_enrichment(conn)
            conn.execute('INSERT INTO billing_config VALUES (?,?)',(enrichment_marker,f'seed={seed};as_of={as_of.isoformat()}'))
            after = counts(conn)
            result = {'status':'upgraded','database':str(db_path),'rows':after,
                      'added':{table:after[table]-before[table] for table in TABLES},
                      'scenarios':scenarios(conn),'validated':True,'retired_records_backup':backup}
            conn.commit()
            return result
        before = counts(conn)
        for table, addition in MAIN_COUNTS.items():
            require(before[table] + addition <= 500,
                    f"Existing {table} rows ({before[table]}) plus this batch ({addition}) exceed 500. "
                    "No data changed. Use --db database/store.db for a separate dataset.")
        for table, column, pattern in [("customers", "customer_id", f"{prefix}-CUST-*"),
                ("items", "item_code", f"{prefix}-ITEM-*"), ("orders", "order_id", f"{prefix}-ORD-*"),
                ("promotions", "promo_code", f"{prefix}-*")]:
            require(not conn.execute(f"SELECT 1 FROM {table} WHERE {column} GLOB ?", (pattern,)).fetchone(),
                    f"Existing reserved identifiers found in {table}; use a fresh database.")
        conn.executemany("INSERT OR IGNORE INTO billing_config(key,value) VALUES (?,?)",
                         [("tax_rate", "7.50"), ("shipping_amount", "12.50"), ("currency", "USD")])
        customers = [f"{prefix}-CUST-{i:04d}" for i in range(1, 401)]
        for i, cid in enumerate(customers, 1):
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            company = f"{rng.choice(['Cedar', 'Harbor', 'Maple', 'Summit', 'Willow'])} {rng.choice(['Studio', 'Labs', 'Services', 'Trading'])} {i:03d}"
            conn.execute("INSERT INTO customers VALUES (?,?,?,?)",
                         (cid, name, company if i % 5 else None, f"customer{i:04d}@example.com"))
        opening_stock = {}
        item_codes = []
        for i in range(1, 401):
            code = f"{prefix}-ITEM-{i:04d}"
            name, base_cents = PRODUCTS[(i - 1) % len(PRODUCTS)]
            selling = money(Decimal(base_cents + rng.randint(-200, 800)) / 100)
            cost = money(selling * Decimal("0.60"))
            stock = rng.randint(40, 180)
            opening_stock[code] = stock
            item_codes.append(code)
            conn.execute("INSERT INTO items VALUES (?,?,?,?,?,?)",
                         (f"{prefix}-SKU-{i:04d}", code, f"{name} - Model {i:03d}", str(cost), str(selling), stock))
            if i <= 80:
                for factor, start, end in [
                    (Decimal("0.85"), as_of-timedelta(days=365), as_of-timedelta(days=121)),
                    (Decimal("0.92"), as_of-timedelta(days=120), as_of-timedelta(days=31)),
                    (Decimal("1.00"), as_of-timedelta(days=30), None),
                ]:
                    conn.execute("INSERT INTO price_history(item_code,unit_price,effective_from,effective_to) VALUES (?,?,?,?)",
                                 (code, str(money(selling * factor)), start.isoformat(), end.isoformat() if end else None))
        promos = []
        for kind, values in [("percent", [5, 10, 15, 20, 25, 30]), ("flat", [5, 10, 20, 40, 75, 100])]:
            for value in values:
                code = f"{prefix}-{kind.upper()}-{value}"
                promos.append(code)
                conn.execute("INSERT INTO promotions(promo_code,description,discount_type,discount_value,expires_on) VALUES (?,?,?,?,?)",
                             (code, f"{kind.capitalize()} discount {value}", kind, str(value), (as_of+timedelta(days=365)).isoformat()))
        rng.shuffle(item_codes)
        cursor = 0
        refunded = {i: ("full" if n % 2 == 0 else "partial")
                    for n, i in enumerate(rng.sample(range(1, 301), 60))}
        refund_counts = {"full": 0, "partial": 0}
        for i in range(1, 351):
            oid = f"{prefix}-ORD-{i:04d}"
            placed = i <= 300
            order_date = as_of - timedelta(days=rng.randint(0, 179)) if placed else as_of
            promo = promos[(i-1) % len(promos)] if i % 4 != 0 else None
            conn.execute("INSERT INTO orders(order_id,order_date,customer_id,subtotal,charged_price,promo_code) VALUES (?,?,?,0,0,?)",
                         (oid, order_date.isoformat(), rng.choice(customers), promo))
            subtotal = Decimal(0)
            for _ in range(2 if i <= 100 else 1):
                code = item_codes[cursor % len(item_codes)]
                cursor += 1
                quantity = rng.randint(1, 6)
                item = conn.execute("SELECT * FROM items WHERE item_code=?", (code,)).fetchone()
                price = price_for(conn, item, order_date.isoformat())
                amount = money(price * quantity)
                subtotal += amount
                conn.execute("INSERT INTO order_requests(order_id,item_code,quantity) VALUES (?,?,?)", (oid, code, quantity))
                if placed:
                    conn.execute("INSERT INTO order_fulfilled(order_id,item_code,quantity) VALUES (?,?,?)", (oid, code, quantity))
                    conn.execute("INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) VALUES (?,?,?,?,?)",
                                 (oid, code, quantity, str(price), str(amount)))
                    updated = conn.execute("UPDATE items SET availablequantity=availablequantity-? WHERE item_code=? AND availablequantity>=?",
                                           (quantity, code, quantity))
                    require(updated.rowcount == 1, f"Insufficient stock for {code}.")
            values = totals(conn, subtotal, promo, check_expiry=False)
            update_totals(conn, oid, values)
            if placed:
                conn.execute("UPDATE orders SET order_invoice=?,order_status='placed' WHERE order_id=?", (f"INV-{prefix}-{as_of.year}-{i:04d}", oid))
            total = money(values["charged_price"])
            if i in refunded and total > 0:
                kind = refunded[i]
                refund = total if kind == "full" else money(total * Decimal("0.25"))
                if refund > 0:
                    reason = "Full return - recorded adjustment" if kind == "full" else "Partial credit for damaged packaging - recorded adjustment"
                    conn.execute("UPDATE orders SET refund_amount=?,refund_status='recorded',refund_reason=? WHERE order_id=?",
                                 (str(refund), reason, oid))
                    refund_counts[kind] += 1
        audit(conn, opening_stock)
        enrich(conn, as_of)
        audit_enrichment(conn)
        conn.execute("INSERT INTO billing_config(key,value) VALUES (?,?)", (marker, f"seed={seed};as_of={as_of.isoformat()}"))
        conn.execute("INSERT INTO billing_config(key,value) VALUES (?,?)", (enrichment_marker, f"seed={seed};as_of={as_of.isoformat()}"))
        after = counts(conn)
        result = {"status": "inserted", "database": str(db_path), "rows": after,
                  "added": {table: after[table]-before[table] for table in TABLES},
                  "placed_orders_added": 307, "draft_orders_added": 51,
                  "ledger_refunds_added": refund_counts, "validated": True,
                  'scenarios':scenarios(conn),'retired_records_backup':backup}
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


class PasswordSetupError(RuntimeError):
    pass


def generate(db_path: Path, seed: int, as_of: date, progress=None, password_workers=PASSWORD_WORKERS):
    if not isinstance(password_workers, int) or isinstance(password_workers, bool) or not 1 <= password_workers <= 4:
        raise ValueError('Password workers must be between 1 and 4.')
    started = perf_counter()
    if progress:
        progress(f'Preparing database: {db_path.resolve()}')
    result = generate_records(db_path, seed, as_of)
    if progress:
        progress(f"Billing records saved: {result['rows']['customers']} customers, "
                 f"{result['rows']['items']} products, {result['rows']['orders']} orders.")
    conn = None
    try:
        conn = sqlite3.connect(db_path.resolve(), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        added = seed_customer_passwords(conn, password_workers, progress)
        result['customer_passwords_added'] = added
        result['rows']['customer_credentials'] = conn.execute('SELECT COUNT(*) FROM customer_credentials').fetchone()[0]
        if 'added' in result:
            result['added']['customer_credentials'] += added
    except Exception as exc:
        raise PasswordSetupError('Billing records and completed password batches are saved. '
                                 'Rerun the same command to finish missing passwords.') from exc
    finally:
        if conn is not None:
            conn.close()
    result['elapsed_seconds'] = round(perf_counter() - started, 3)
    if progress:
        progress(f"Finished in {result['elapsed_seconds']:.2f} seconds.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=settings.BILLING_DB, help="SQLite file; relative paths use the project folder.")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic random seed.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(), help="Order date anchor, YYYY-MM-DD.")
    parser.add_argument('--password-workers', type=int, choices=range(1, 5), default=PASSWORD_WORKERS,
                        help='Parallel password hashing workers (default: up to 4 CPU cores).')
    args = parser.parse_args()
    path = args.db if args.db.is_absolute() else settings.ROOT / args.db
    try:
        result = generate(path, args.seed, args.as_of,
                          progress=lambda message: print(message, file=sys.stderr, flush=True),
                          password_workers=args.password_workers)
        print(json.dumps(result, indent=2))
    except PasswordSetupError as exc:
        parser.exit(1, f'Password setup stopped: {exc}\nCause: {exc.__cause__}\n')
    except KeyboardInterrupt:
        parser.exit(130, '\nStopped. Rerun the same command to resume. Completed records and password batches are saved.\n')
    except (ValueError, sqlite3.Error, ArithmeticError) as exc:
        parser.exit(1, f"Seed records rolled back: {exc}\nRetired-table cleanup, if applied, is a separate migration.\n")


if __name__ == "__main__":
    main()
