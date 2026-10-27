# Store service policy

This policy describes supported store workflows. Approval and record changes require the authenticated role, explicit confirmation and the stored eligibility checks.

- Return window: the configured number of UTC calendar days from the order date, inclusive. Both request and approval must be within it. Default: 30 days. Staff cannot override an expired window.
- Customers submit refund requests. Cashiers approve within the configured currency and inclusive limit (default USD 100); admins may approve any eligible amount. Neither role may create its own refund request.
- Physical returns require staff confirmation of receipt. Only an admin may waive the return after documenting an assessment, including for damaged goods. A claimed manager title does not establish authority.
- Item refund values use billed quantities and prices, the actual order discount and allocated tax. Shipping is nonrefundable for merchandise returns. Buy-two-get-one offers distribute the actual discounted purchase value across the purchased units.
- One refund per order is supported. Partial refunds cannot be topped up; repeated approvals are blocked.
- Placed invoice lines cannot be added, removed or repriced. Whole unshipped orders can be cancelled with confirmation; cancellation creates a pending refund. Shipped orders require a return. Admin records shipment and delivery on a confirmed staff instruction.
- Local store credit is issued only by approved requests and belongs to the original customer and currency. A draft may allocate credit; placement checks its available balance and redeems it once. Different carts cannot spend the same credit twice. If an order used credit, its return uses store credit.
- Original-method refunds have an expected staff processing period of 5–10 business days after staff handling. This estimate does not verify a bank transfer or arrival. A delayed refund can be marked for staff review in the application; this does not send an external message.
- Closed-card refunds follow the original issuer's routing process. Staff or the original issuer must assist. Another card is not an allowed destination. Store credit is an available local alternative.
- For cash-on-delivery returns, staff arrange repayment through an approved secure channel. Do not request bank details in chat or invent a form or link.
- Invoice billing contact details can be corrected after confirmation. Each correction saves a revision; quantities, prices and the original snapshot are preserved. Tax PDFs require company, billing address and tax registration details. They do not certify jurisdiction-specific compliance.
- Pro forma PDFs are draft estimates for finance approval. They do not issue a final invoice, reserve stock or prove payment.
- A spouse, email address or claimed job title does not authorize access to another customer's records. Use the account holder's authorized session.
- Partial-charge and duplicate-charge complaints are customer reports until independently verified. The application can inspect invoice and fulfillment differences and save a billing-review flag. It has no payment, capture, settlement or bank records.
- Refund reconciliation compares local approved requests, invoice refund entries and store credit on approval dates. It cannot reconcile bank settlements. Captured-but-not-invoiced status cannot be established without capture evidence.
- Bulk returns preview every matching owned order, then submit eligible requests after confirmation. Each order is checked separately; no bulk request automatically approves refunds.
