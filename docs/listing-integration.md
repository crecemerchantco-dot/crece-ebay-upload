# Listing integration: first milestone

## Remote validation milestone — 2026-11-15

`/validate` accepts a file upload containing `format: crece-ebay-listing-v1`
and `listing`, an eBay `CreateListingInput`. This is a prepared artifact, not
something the seller should manually assemble. US, fixed-price, single-item
packages are supported. The previously scheduled scale is excluded from tests.

The server replaces both `options` and `idempotencyKey`; every request explicitly
uses `VALIDATE`, with a fixed query and fixed eBay GraphQL endpoint. There is no
EXECUTE path. Only `CreateListingValidationSuccess` counts as a pass. HTTP 200,
GraphQL errors, missing data, and unexpected operation result types cannot pass.
No automatic retries. No listing IDs are extracted or saved from validation.

The separate **Connect eBay for listing checks** action requests `sell.listing`
alongside inventory and account-read access. This permission itself supports
listing writes; the deployed code restricts its use to validation. App keyset
eligibility and a real seller validation remain unverified until the seller
connects and checks a complete new-item package. Tokens are still process-local.

Validation: 24 mocked unit/route tests; query parsed and validated against the
official `/develop/api/spec/listing.graphqls` schema downloaded 2026-11-15.
No real seller token or publishing call was used in development.

Before enabling publication: owner authentication, encrypted durable seller-token
storage, a durable per-SKU operation ledger, reconciliation after unknown outcomes,
catalog writeback, and a complete live validation of a new item are still needed.
Do not turn validation into execution by changing a default or accepting a file's
mode. Build a distinct publishing action and persist its intent before sending.

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

## Bundled photo upload

`/validate` also accepts a ZIP with `listing.json` and a manifest `photos` array of ordered paths under `photos/`. Archives are never extracted; duplicate, unexpected, traversal, oversized and excessive entries are rejected. The existing Media API uploader hosts actual photographs first. Any photo failure stops before the listing check. The check remains VALIDATE-only. Seller-scoped `/validate/package` downloads the assembled manifest and returned photo URLs for catalog reconciliation; it contains no credentials. This in-memory download is lost on restart.

Verification: 28 tests pass, including ordered ZIP handling, archive rejection, failed-photo short circuit, and session isolation for downloads. No live photo or listing check has run for this workflow yet.

## Narrow approved-book scheduling test — 2026-11-15

`/publish/kitcho` is a separate seller-authenticated, CSRF-protected action for
MW-2026-296 at $29.95 plus calculated Media Mail, Best Offer on, scheduled
2026-11-16T12:00:00Z (7 a.m. America/Detroit). Matt disclosed his desired day
after the price was shown. `approved/kitcho.json` persists this exact intent,
its payload digest and a stable 32-hex idempotency key. This is not a generic
publisher: request bodies cannot choose an item, price or schedule.

Before creation, search active and scheduled eBay listings by the approved SKU.
Any incomplete or rejected search blocks creation. Existing matches are returned
without a second creation. Revalidate with eBay, then send the approved EXECUTE
request once. Every retry uses the same persisted key and first reconciles eBay
state. Unknown outcomes do not trigger automatic retries. Retrieve listing ID,
observed lifecycle state/start time, price, photos and quantity for the receipt.
A creation response is not proof of a scheduled/live state. Receipt download is
seller-session scoped. The assistant must reconcile the catalog from the receipt.

No durable seller-token storage, external background job, new database or paid
service is introduced. eBay holds the future schedule itself. The immutable
approval file is a durable intent, not a durable outcome ledger; receipts remain
in server memory until downloaded. General unattended publication and automatic
catalog writeback still require the stronger storage/auth/reconciliation design
above. Do not expand this test by accepting arbitrary file content for execution.

## Reusable private workspace (`/sell`)

The Kitcho route remains a separate, immutable prior test. The reusable workspace
is disabled until all three settings are present:

- `APP_ACCESS_PASSWORD_HASH`: Werkzeug password hash of a strong owner password.
  Generate locally with `werkzeug.security.generate_password_hash`; keep both the
  password and hash out of this repository. Enter the hash privately in Render.
- `LISTING_DATABASE_URL`: PostgreSQL connection URL for a dedicated receipt journal.
  For a Render database use the internal connection URL in the same workspace and
  region (Virginia). No fallback to ephemeral SQLite or process-memory receipts.
- `GOOGLE_SERVICE_ACCOUNT_JSON`: a dedicated Google service account credential,
  entered privately in Render. Enable Google Sheets API in its Google Cloud
  project. Share only master spreadsheet
  `17NcKyscAKXdIbUDoWJsMLFNewUehYzjcrnJSCfliUW8` with that account's email as Editor.
  Do not enable domain-wide delegation. Do not paste its private key into chat.

No new paid resource is provisioned by this change. Confirm the price in Render
before creating a database. At implementation time the smallest paid Postgres
compute is $6/month plus $0.30/GB/month storage; verify current pricing at setup.
The free web service can remain unchanged. A free expiring database is unsuitable
as the permanent submission journal.

The workspace uploads the existing ZIP format, checks the SKU against the master
catalog, uploads real photos, and shows the immutable price, live policy names, shipping rates and offer
thresholds before asking for a day. A changed policy requires a new review. A blank time uses 07:00 America/Detroit; a day is
mandatory unless Publish now is explicitly pressed. Client form values cannot
change price or listing payload. Price reviews expire after one hour.

On submission: recheck inventory, check active and scheduled eBay SKUs, validate,
recheck inventory, commit the intent and stable creation key, execute once, commit
the returned ID, retrieve and compare observed SKU, price, quantity, title, images
and start, then save the verified outcome before updating the catalog. PostgreSQL
session advisory locks serialize this app's requests for a SKU across processes.
They do not lock other marketplaces, Seller Hub, or human spreadsheet edits.

A timeout or crash after intent commit remains unresolved. Recovery only reads
status; it never silently executes another create. If no match is found, reconcile
with Seller Hub manually. An existing eBay SKU with no saved intent blocks creation
and requires manual linkage. No automatic deletion, cancellation, relisting or
reset of an intent is implemented. Catalog failures leave a durable pending receipt
and can retry only the catalog write. Catalog writes resolve rows by SKU, check live
headers, preserve cost/formula columns and leave Date Listed blank while scheduled.
The companion channel ledger is upserted by SKU plus eBay; a conflicting URL blocks
updates. Receipts never include seller OAuth tokens or Google credentials.

Verification is deliberately conservative. eBay-normalized image URLs or a changed
listing price/status may require manual reconciliation. A scheduled receipt records
an observed schedule, not proof of going live later. There is no unattended worker,
automatic schedule monitor or Facebook publishing API in this change. The assistant
continues to handle research, duplicate catalog intake, pricing and Facebook channel
recommendation before preparing the upload package.

### Setup acceptance checks

1. Before configuring secrets, `/sell` redirects to a private login page and cannot
   publish. Existing uploader/validation/Kitcho pages continue to work.
2. Configure the three settings and deploy. Sign in privately and connect eBay.
3. Use a genuinely new cataloged item, review the disclosed price, and select a day.
   Do not reuse Kitcho or the National scale as a creation test.
4. Confirm the saved receipt and spreadsheet SKU, ID, status, price, photos and URL.
   Test recovery of a pending catalog result without another EXECUTE call.
5. Confirm journal persistence across a restart. eBay login must be renewed because
   OAuth tokens intentionally remain only in process memory.

Automated tests mock eBay/Sheets and the journal. They cover the price/day gate,
Eastern DST behavior, timeout recovery, failed-catalog recovery, wrong observed
price, duplicate/sold catalog guards, schema-change blocks and owner/CSRF gates.
A real database and Google service-account smoke test remain required at setup.
