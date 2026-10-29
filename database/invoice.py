import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.sax.saxutils import escape
from config import settings
from database.connection import connect


def invoice_path(invoice_number, customer_id):
    digest = hashlib.sha256(f"{customer_id}:{invoice_number}".encode()).hexdigest()
    return settings.INVOICE_DIR / f"{digest}.pdf"


def generate_invoice(invoice_number, customer_id, *, connection=None):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from mcp_server.tools.billing_tools import BillingError, money
    document_type='proforma' if invoice_number.startswith('PROFORMA-') else 'tax' if invoice_number.startswith('TAX-') else 'invoice'
    underlying=invoice_number[9:] if document_type=='proforma' else invoice_number[4:] if document_type=='tax' else invoice_number
    conn = connection if connection is not None else connect(readonly=True)
    try:
        if connection is None:
            conn.execute("BEGIN")
        lookup='o.order_id' if document_type=='proforma' else 'o.order_invoice'
        order = conn.execute("SELECT o.*,c.name,c.company,c.email FROM orders o JOIN customers c "
                             f"ON c.customer_id=o.customer_id WHERE {lookup}=? AND o.customer_id=?",
                             (underlying, customer_id)).fetchone()
        if order is None:
            raise BillingError("Invoice not found for this customer.")
        lines = conn.execute("SELECT l.*,i.name FROM invoice_lines l JOIN items i ON i.item_code=l.item_code "
                             "WHERE l.order_id=? ORDER BY l.id", (order["order_id"],)).fetchall()
        if document_type=='proforma':
            from mcp_server.tools.billing_tools import order_row,price_for,totals
            if order_row(conn,order['order_id'],customer_id)['order_status'] != 'draft': raise BillingError('Pro forma is for an active unplaced draft.')
            lines=[]
            for r in conn.execute('SELECT r.*,i.name,i.sellingprice FROM order_requests r JOIN items i ON i.item_code=r.item_code WHERE r.order_id=? ORDER BY r.id',(order['order_id'],)):
                price=price_for(conn,r,order['order_date'])
                lines.append({'name':r['name'],'item_code':r['item_code'],'qty_billed':r['quantity'],'unit_price_billed':price,'line_total':money(price*r['quantity'])})
            current=totals(conn,sum((money(r['line_total']) for r in lines),money(0)),order['promo_code'])
            order={**dict(order),**current}
        currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()
        currency = currency[0] if currency else "USD"
        metadata = conn.execute('SELECT currency,due_date,customer_snapshot FROM order_details WHERE order_id=?', (order['order_id'],)).fetchone()
        currency = metadata['currency'] if metadata else currency
        contact = json.loads(metadata['customer_snapshot']) if metadata and metadata['customer_snapshot'] else dict(order)
        profile=conn.execute('SELECT * FROM customer_billing_details WHERE customer_id=?',(customer_id,)).fetchone()
        if profile:
            for key in ('billing_address','tax_registration_number'):
                if document_type=='proforma': contact[key]=profile[key]
        revision=conn.execute('SELECT id,contact_json FROM invoice_revisions WHERE order_id=? ORDER BY id DESC LIMIT 1',(order['order_id'],)).fetchone()
        if revision and document_type!='proforma': contact=json.loads(revision['contact_json'])
        credit=sum((money(-r[0]) for r in conn.execute("SELECT amount FROM store_credit_ledger WHERE order_id=? AND kind='order_redemption'",(order['order_id'],))),money(0))
        delivery = conn.execute('SELECT address_snapshot FROM order_delivery WHERE order_id=? AND customer_id=?',
                                (order['order_id'],customer_id)).fetchone()
        delivery = json.loads(delivery[0]) if delivery else None
        if document_type=='tax' and not all(contact.get(k) for k in ('company','billing_address','tax_registration_number')):
            raise BillingError('Tax invoice needs company, billing address and tax registration number; record or correct them first.')
    finally:
        if connection is None:
            conn.close()
    if not lines:
        raise BillingError("Invoice has no persisted lines.")
    styles = getSampleStyleSheet()
    def p(text, style="BodyText"):
        return Paragraph(escape(str(text)), styles[style])
    story = [p({"invoice":"INVOICE","tax":"TAX INVOICE","proforma":"PRO FORMA INVOICE"}[document_type], "Title"), p(invoice_number, "Heading2"),
             p(f"Order {order['order_id']} | Order date {order['order_date']}"), Spacer(1, 16),
             p(contact["name"], "Heading2")]
    for field in ("company", "email", "customer_id", "billing_address", "tax_registration_number"):
        if contact.get(field):
            story.append(p(("Tax registration: " if field=="tax_registration_number" else "")+str(contact[field])))
    if revision and document_type!='proforma': story.append(p(f"Billing details revision {revision['id']}"))
    if delivery:
        story.append(p('Delivery address', 'Heading3'))
        for field in ('full_name','phone','address_line1','address_line2','city','state','postal_code','country','landmark'):
            if delivery.get(field):
                story.append(p(delivery[field]))
    if document_type=='proforma': story.append(p("Estimate for approval. Goods are not reserved; this is not an issued tax invoice."))
    story.append(p('Due date: ' + ((metadata['due_date'] if metadata else None) or 'Not recorded')))
    story.append(Spacer(1, 18))
    data = [[p(x) for x in ("Item", "Qty", "Unit price", "Amount")]]
    for line in lines:
        data.append([p(f"{line['name']} ({line['item_code']})"), p(line["qty_billed"]),
                     p(money(line["unit_price_billed"])), p(money(line["line_total"]))])
    table = Table(data, colWidths=[255, 40, 90, 90], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7edf5")),
                               ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#d9d9d9")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 9),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    story.extend([table, Spacer(1, 20)])
    for label, field in [("Subtotal", "subtotal"), ("Discount", "discount_applied"),
                         ("Tax", "tax_amount"), ("Shipping", "shipping_amount"), ("Total", "charged_price")]:
        story.append(p(f"{label}: {currency} {money(order[field])}", "Heading3" if field == "charged_price" else "BodyText"))
    if order["refund_amount"] is not None:
        story.extend([Spacer(1, 12), p(f"Ledger refund: {currency} {money(order['refund_amount'])} ({order['refund_status']})"),
                      p(order["refund_reason"] or ""), p("This ledger entry does not confirm a payment-provider refund.")])
    story.append(Spacer(1, 12))
    net = money(order['charged_price']) - money(order['refund_amount'] or 0)
    story.append(p(f'Invoice total after recorded refunds: {currency} {net}'))
    if credit:
        story.append(p(f'Store credit applied: {currency} {credit}'))
        story.append(p(f'Invoice amount after store credit: {currency} {money(order["charged_price"])-credit}'))
    story.append(p('Payment status and outstanding balance are not tracked. This invoice is not proof of payment.'))
    path = invoice_path(invoice_number, customer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, suffix=".pdf", delete=False) as handle:
            temp = Path(handle.name)
        SimpleDocTemplate(str(temp), pagesize=A4, rightMargin=60, leftMargin=60,
                          title=f"Invoice {invoice_number}").build(story)
        os.replace(temp, path)
    finally:
        if temp and temp.exists():
            temp.unlink()
    return {"invoice_number": invoice_number, "customer_id": customer_id,
            "filename": "invoice.pdf", "ready": True}
