import asyncio
from contextlib import closing
from datetime import date
import json
import sqlite3

import pytest
from config import settings
import generate_seed as seed
from database.connection import initialize
from database.migrations import remove_retired_tables, RETIRED_TABLES
from agent.billing_agent import parameters, decode_result
from autogen_ext.tools.mcp import McpWorkbench


def snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {table:conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() for table in tables}


def test_full_seed_is_consistent_and_idempotent(tmp_path):
    path = tmp_path/'full.db'
    result = seed.generate(path,42,date(2026,10,13))
    assert result['rows']['orders']==358
    assert result['customer_passwords_added']==400
    assert result['rows']['customer_credentials']==400
    assert result['rows']['product_details']==400 and result['rows']['product_attributes']==400
    assert result['rows']['product_relations']==480
    assert 'payments' not in result['rows'] and 'credit_notes' not in result['rows']
    assert result['rows']['billing_audit'] > 0
    before = snapshot(path)
    assert not set(RETIRED_TABLES)&set(before)
    repeated = seed.generate(path,123,date(2030,1,1))
    assert repeated['status']=='already_seeded'
    assert repeated['customer_passwords_added']==0
    assert snapshot(path)==before
    with closing(sqlite3.connect(path)) as conn:
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert conn.execute('SELECT count(*) FROM orders WHERE order_invoice IS NOT NULL').fetchone()[0]==307
        assert not conn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()
        assert not conn.execute('SELECT 1 FROM orders WHERE COALESCE(refund_amount,0)>charged_price').fetchone()
        from api.customer_auth import verify_password
        encoded = conn.execute('SELECT customer_id,password_hash FROM customer_credentials ORDER BY customer_id').fetchall()
        assert len({row[1] for row in encoded})==400
        assert all(value.startswith('pbkdf2_sha256$600000$') for _,value in encoded)
        for customer_id,value in (encoded[0], encoded[-1]):
            assert not verify_password(customer_id+'-Store!2026', value)
            assert customer_id+'-Store!2026' not in value
        assert not conn.execute('SELECT c.customer_id FROM customers c LEFT JOIN customer_credentials a ON a.customer_id=c.customer_id WHERE a.customer_id IS NULL').fetchall()
        assert not list(path.parent.glob('*.csv'))


def test_v1_upgrades_without_rewriting_original_invoices(tmp_path,monkeypatch):
    path = tmp_path/'legacy_seed.db'
    with monkeypatch.context() as patch:
        patch.setattr(seed,'enrich',lambda *_:None)
        patch.setattr(seed,'audit_enrichment',lambda *_:None)
        seed.generate(path,42,date(2026,10,8))
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM billing_config WHERE key=?',(seed.ENRICHMENT_MARKER,))
    before = snapshot(path)
    result = seed.generate(path,42,date(2026,10,13))
    assert result['status']=='upgraded'
    after = snapshot(path)
    for table in ('customers','orders','invoice_lines','order_requests','order_fulfilled','price_history','promotions'):
        assert set(before[table])<=set(after[table]), table
    new_sold = {}
    old_fulfilled=set(before['order_fulfilled'])
    for row in after['order_fulfilled']:
        if row not in old_fulfilled: new_sold[row[2]]=new_sold.get(row[2],0)+row[3]
    for old,current in zip(before['items'],after['items']):
        assert old[:-1]==current[:-1]
        assert current[-1]==old[-1]-new_sold.get(old[1],0)


def test_failed_seed_preserves_existing_records():
    with sqlite3.connect(settings.BILLING_DB) as conn:
        conn.execute("INSERT INTO customers VALUES ('STORE-CUST-0001','Existing',NULL,NULL)")
    before = snapshot(settings.BILLING_DB)
    with pytest.raises(ValueError,match='Existing reserved identifiers'):
        seed.generate(settings.BILLING_DB,42,date(2026,10,13))
    assert snapshot(settings.BILLING_DB)==before


def test_generator_uses_neutral_labels():
    import re
    text = (settings.ROOT/'generate_seed.py').read_text(encoding='utf-8')
    assert not re.search(r'fake|fictional|demo',text,re.IGNORECASE)


def test_existing_large_dataset_gets_missing_passwords_without_resets(monkeypatch):
    from api.customer_auth import set_password, login
    import database.service_seed as service_seed
    with sqlite3.connect(settings.BILLING_DB) as conn:
        conn.executemany('INSERT INTO customers VALUES (?,?,NULL,NULL)',
                         [('FAKE-CUST-0001','Alex'),('FAKE-CUST-0002','Jamie')])
        conn.execute("INSERT INTO billing_config VALUES ('fake_seed_v2','existing batch')")
    set_password('FAKE-CUST-0002','Previously chosen password')
    existing = snapshot(settings.BILLING_DB)
    monkeypatch.setattr(service_seed,'seed_service_scenarios',lambda *_:None)
    result = seed.generate(settings.BILLING_DB,42,date.today())
    assert result['status']=='already_seeded' and result['customer_passwords_added']==1
    after = snapshot(settings.BILLING_DB)
    assert all(after[table]==rows for table,rows in existing.items() if table!='customer_credentials')
    assert set(existing['customer_credentials']) <= set(after['customer_credentials'])
    assert not login('FAKE-CUST-0001','FAKE-CUST-0001-Store!2026')
    set_password('FAKE-CUST-0001','A separately chosen account password')
    assert login('FAKE-CUST-0001','A separately chosen account password')
    assert login('FAKE-CUST-0002','Previously chosen password')
    assert not login('FAKE-CUST-0002','FAKE-CUST-0002-Store!2026')
    assert seed.generate(settings.BILLING_DB,42,date.today())['customer_passwords_added']==0


def test_billing_records_are_visible_before_password_setup(tmp_path,monkeypatch):
    from api import customer_auth
    path = tmp_path/'failed.db'
    def fail(_):
        # A separate connection can already read the committed, valid dataset.
        with sqlite3.connect(path) as reader:
            assert reader.execute('SELECT COUNT(*) FROM customers').fetchone()[0]==400
            assert reader.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==358
            assert not reader.execute('PRAGMA foreign_key_check').fetchall()
        raise ValueError('Password hashing unavailable')
    monkeypatch.setattr(customer_auth,'password_hash',fail)
    progress=[]
    with pytest.raises(seed.PasswordSetupError,match='completed password batches are saved'):
        seed.generate(path,42,date.today(),progress=progress.append)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]==400
        assert conn.execute('SELECT COUNT(*) FROM customer_credentials').fetchone()[0]==0
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert progress[0].startswith('Preparing database:')
    assert any(line.startswith('Billing records saved:') for line in progress)
    assert any(line.startswith('Preparing 400 customer passwords') for line in progress)


def test_interrupted_password_batches_resume_without_resets(monkeypatch):
    from api import customer_auth
    original_hash = customer_auth.password_hash
    with sqlite3.connect(settings.BILLING_DB) as conn:
        conn.executemany('INSERT INTO customers VALUES (?,?,NULL,NULL)',
                         [('STORE-CUST-0001','Alex'),('STORE-CUST-0002','Jamie'),('STORE-CUST-0003','Sam')])
    calls=[]
    def interrupted(password):
        calls.append(password)
        if len(calls)>1:
            raise KeyboardInterrupt()
        return original_hash(password)
    monkeypatch.setattr(seed,'PASSWORD_BATCH_SIZE',1)
    monkeypatch.setattr(customer_auth,'password_hash',interrupted)
    with sqlite3.connect(settings.BILLING_DB) as conn:
        with pytest.raises(KeyboardInterrupt):
            seed.seed_customer_passwords(conn,workers=1)
    with sqlite3.connect(settings.BILLING_DB) as reader:
        first=reader.execute('SELECT customer_id,password_hash FROM customer_credentials').fetchall()
        assert len(first)==1 and first[0][0]=='STORE-CUST-0001'
    monkeypatch.setattr(customer_auth,'password_hash',original_hash)
    with sqlite3.connect(settings.BILLING_DB) as conn:
        assert seed.seed_customer_passwords(conn,workers=2)==2
        assert seed.seed_customer_passwords(conn,workers=2)==0
        after=conn.execute('SELECT customer_id,password_hash FROM customer_credentials ORDER BY customer_id').fetchall()
    assert after[0]==first[0]
    assert len(after)==3
    assert all(not customer_auth.verify_password(cid+'-Store!2026',encoded) for cid,encoded in after)


def test_password_setup_preserves_a_password_changed_while_hashing(monkeypatch):
    from api import customer_auth
    original_hash = customer_auth.password_hash
    with sqlite3.connect(settings.BILLING_DB) as conn:
        conn.execute("INSERT INTO customers VALUES ('STORE-CUST-0001','Alex',NULL,NULL)")
    changed = False
    def concurrently_changed(password):
        nonlocal changed
        if not changed:
            changed = True
            customer_auth.set_password('STORE-CUST-0001','A customer chosen password')
        return original_hash(password)
    monkeypatch.setattr(customer_auth,'password_hash',concurrently_changed)
    with sqlite3.connect(settings.BILLING_DB) as conn:
        assert seed.seed_customer_passwords(conn)==0
    assert customer_auth.login('STORE-CUST-0001','A customer chosen password')
    assert not customer_auth.login('STORE-CUST-0001','STORE-CUST-0001-Store!2026')


def test_seed_initial_passwords_are_independent_unexported_secrets(monkeypatch, capsys):
    from api import customer_auth
    original_hash = customer_auth.password_hash
    prepared = []
    def record(password):
        prepared.append(password)
        return original_hash(password)
    monkeypatch.setattr(customer_auth, 'password_hash', record)
    with sqlite3.connect(settings.BILLING_DB) as conn:
        conn.executemany('INSERT INTO customers VALUES (?,?,NULL,NULL)',
                         [('STORE-CUST-0001', 'Alex'), ('STORE-CUST-0002', 'Sam')])
        conn.commit()
        progress = []
        assert seed.seed_customer_passwords(conn, progress=progress.append) == 2
        stored = conn.execute('SELECT password_hash FROM customer_credentials').fetchall()
    assert len(prepared) == len(set(prepared)) == 2
    assert all(len(value) >= 40 and 'STORE-CUST' not in value for value in prepared)
    output = repr(progress) + repr(stored) + capsys.readouterr().out
    assert all(value not in output for value in prepared)


def legacy_tables(conn):
    conn.executescript('''CREATE TABLE billing_cases(case_id TEXT PRIMARY KEY, summary TEXT);
        CREATE TABLE billing_case_events(event_id TEXT PRIMARY KEY,case_id TEXT REFERENCES billing_cases(case_id),note TEXT);
        CREATE TRIGGER case_events_no_delete BEFORE DELETE ON billing_case_events
        BEGIN SELECT RAISE(ABORT,'No event deletion'); END;
        CREATE TABLE billing_disputes(dispute_id TEXT PRIMARY KEY,reason TEXT);
        INSERT INTO billing_cases VALUES ('OLDCASE','Keep this historical record');
        INSERT INTO billing_case_events VALUES ('OLDEVENT','OLDCASE','Original note');
        INSERT INTO billing_disputes VALUES ('OLDDISPUTE','Original dispute');
        CREATE TABLE payments(payment_id TEXT PRIMARY KEY,amount_cents INTEGER);
        CREATE TABLE credit_notes(credit_id TEXT PRIMARY KEY,amount_cents INTEGER);
        INSERT INTO payments VALUES ('OLDPAY',1000);
        INSERT INTO credit_notes VALUES ('OLDCREDIT',200);
        INSERT INTO billing_audit(action,details_json) VALUES ('historical_change','{}');''')


def test_retired_table_cleanup_backs_up_records_and_does_not_recreate_them(tmp_path):
    with closing(sqlite3.connect(settings.BILLING_DB)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        legacy_tables(conn)
        backup = remove_retired_tables(conn,tmp_path/'backups')
        assert conn.execute("SELECT count(*) FROM customers").fetchone()[0]==4
        assert not set(RETIRED_TABLES)&{r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    with closing(sqlite3.connect(backup)) as conn:
        assert conn.execute('SELECT note FROM billing_case_events').fetchone()[0]=='Original note'
        assert conn.execute('SELECT reason FROM billing_disputes').fetchone()[0]=='Original dispute'
        assert conn.execute('SELECT amount_cents FROM payments').fetchone()[0]==1000
        assert conn.execute('SELECT amount_cents FROM credit_notes').fetchone()[0]==200
    initialize()
    assert not set(RETIRED_TABLES)&set(snapshot(settings.BILLING_DB))
    with closing(sqlite3.connect(settings.BILLING_DB)) as conn:
        assert conn.execute("SELECT count(*) FROM billing_audit WHERE action='historical_change'").fetchone()[0]==1


def test_cleanup_failure_keeps_original_tables(tmp_path):
    invalid = tmp_path/'not-a-directory'
    invalid.write_text('Preserve me')
    with closing(sqlite3.connect(settings.BILLING_DB)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        legacy_tables(conn)
        with pytest.raises(OSError):
            remove_retired_tables(conn,invalid)
        assert conn.execute('SELECT count(*) FROM billing_case_events').fetchone()[0]==1
        assert conn.execute('SELECT amount_cents FROM payments').fetchone()[0]==1000
        assert conn.execute('SELECT amount_cents FROM credit_notes').fetchone()[0]==200


def test_all_twelve_tools_with_generated_database(tmp_path,monkeypatch):
    path = tmp_path/'tools.db'
    result = seed.generate(path,42,date.today())
    monkeypatch.setattr(settings,'BILLING_DB',path)
    scenario = result['scenarios']
    async def run():
        seen = set()
        async with McpWorkbench(parameters()) as wb:
            async def invoke(tool_name,admin=False,owner='STORE-CUST-0001',**arguments):
                response = decode_result(await wb.call_tool(tool_name,{**arguments,
                    'execution_mode':'admin' if admin else 'customer','execution_customer_id':owner}))
                assert response.get('ok'),(tool_name,response)
                seen.add(tool_name)
                return response
            compared = await invoke('compare_products',products=scenario['comparison_items'])
            assert len(compared['products'])==2 and compared['same_recorded_category']
            alternatives = await invoke('find_cheaper_alternatives',product_reference=scenario['cheaper_alternatives_source'])
            assert alternatives['matched_count']>0
            account = decode_result(await wb.call_tool('manage_customer',dict(
                customer_id='DEMO-NEW-001',name='Demo Shopper',confirmed=True,
                execution_mode='customer',execution_customer_id=None)))
            assert not account['ok']  # Account creation belongs to verified registration.
            await invoke('manage_customer',customer_id='STORE-CUST-0001',name='Updated Demo Shopper',action='update',confirmed=True)
            draft = await invoke('request_order',customer_id='STORE-CUST-0001',order_id='TOOL-DEMO-NEW',items=[{'item_code':'STORE-ITEM-0001','quantity':1}])
            await invoke('manage_draft',customer_id='STORE-CUST-0001',order_id=draft['order_id'],action='replace_items',
                         items=[{'item_code':'STORE-ITEM-0001','quantity':2}],confirmed=True)
            await invoke('apply_promotion',customer_id='STORE-CUST-0001',order_id=draft['order_id'],promo_code=scenario['promo_code'],execution_coupon_codes=[scenario['promo_code']])
            placed = await invoke('place_order',customer_id='STORE-CUST-0001',order_id=draft['order_id'])
            assert (await invoke('download_invoice',customer_id='STORE-CUST-0001',invoice_number=placed['invoice_number']))['ready']
            assert (await invoke('process_refund',customer_id='STORE-CUST-0001',order_id=scenario['refund_order_id'],amount='25.00',reason='Fictional demo return'))['refund_status']=='pending_approval'
            assert (await invoke('process_refund',admin=True,action='approve',confirmed=True,return_received=True,customer_id='STORE-CUST-0001',order_id=scenario['refund_order_id'],amount='25.00',reason='Fictional demo return'))['refund_status']=='recorded'
            await invoke('manage_draft',customer_id='STORE-CUST-0001',order_id=scenario['draft_order_id'],action='cancel',reason='Demo cancellation',confirmed=True)
            assert (await invoke('billing_analysis',admin=True))['invoice_count']==308
            anomalies = await invoke('billing_anomalies',admin=True,max_findings=100)
            assert anomalies['counts_by_rule'].get('HIGH_INVOICE_VALUE',0)>0
            assert not any(f['severity']=='error' for f in anomalies['findings'])
            plotted = await invoke('execute_sql_query',admin=True,
                query="SELECT strftime('%Y-%m',o.order_date) month,COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD') currency,ROUND(SUM(o.charged_price),2) revenue FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id WHERE o.order_invoice IS NOT NULL AND COALESCE(d.status,'active')<>'cancelled' GROUP BY month,COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD') ORDER BY month",
                chart={'type':'line','title':'Monthly billed revenue','x':'month','y':['revenue']})
            assert 'chart' in plotted
            own = await invoke('execute_sql_query',query='SELECT DISTINCT customer_id FROM orders')
            assert own['rows']==[['STORE-CUST-0001']]
        assert len(seen)==12
    asyncio.run(run())
