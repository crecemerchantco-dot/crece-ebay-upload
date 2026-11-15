# Listing integration: first milestone

This branch extends the photo uploader with `/checks`, a read-only policy and
category discovery page, plus **local** intake preflight. It has no listing
creation, publishing, scheduling, revision, ending or inventory-write method.
Local preflight is not eBay validation or proof of acceptance.

## Enable after review

Existing photo authorization stays unchanged. Choose **Connect eBay for checks**
on `/checks` to request `sell.account.readonly` in addition to the existing
`sell.inventory` grant. eBay must approve the requested permissions. Never paste
credentials or tokens into chat. Category discovery uses an application token
with the base scope and looks up the current US category-tree ID dynamically.

Test `/checks/policies` for current policy names/IDs and `/checks/category?category_id=163098`
for scale item specifics. Failures are explicit and don't become empty success.
The intake check accepts JSON with sku, title, description, price, quantity,
categoryId, imageUrls, aspects and optional timezone-aware scheduleTime.

## Verification

Run `python -m unittest discover -s tests -v`. Tests use mocked HTTP; no seller
account or real listing is touched. Live seller authorization, developer app
entitlements and endpoint compatibility still require a deployed test.

## Next milestone

1. Add owner authentication and encrypted durable token storage; the current
   prototype still loses seller authorization on restart.
2. Verify the current Listing API schema, app entitlement, validation mode,
   Seller Hub editing compatibility and exact OAuth scopes for this app.
3. Implement remote validation with `options.operationMode=VALIDATE` explicitly
   set. Never allow a default EXECUTE mode in validation-only code.
4. Persist one idempotency key per intended creation, canonical payload hash,
   catalog SKU and operation outcome before adding publication. Reconcile
   ambiguous failures before retries. Do not republish the scheduled scale.
5. Add Google Sheets writeback with observed listing ID/state and a recovery
   queue so an eBay success followed by a Sheets failure cannot duplicate a sale.

## Official references checked 2026-11-15

- https://developer.ebay.com/develop/guides/sell/listing-creation
- https://developer.ebay.com/develop/guides/sell/listing-metadata-guide
- https://developer.ebay.com/develop/guides/sell/account-management-guide
- https://developer.ebay.com/api-docs/sell/static/inventory/managing-image-media.html

Repository: `crecemerchantco-dot/crece-ebay-upload`. The Render main branch
auto-deploys. Use a feature branch and review tests before merging. Live eBay
connection checks require the seller to authorize the read-only account scope.
