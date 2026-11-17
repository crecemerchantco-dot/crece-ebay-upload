"""Individually approved, committed sale intents for the manual no-database flow.

Only the explicit registry below is executable. This is not an arbitrary-package
publisher. Outcomes are reconciled by the operator with the master catalog.
"""
import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
import requests
from flask import abort, request, session, url_for, Response
from ebay_listing import LISTING_SCOPE, validate_listing
from ebay_publish import PublishError, post, verify, CREATE_QUERY
from resale_workflow import find_existing, check_observed
from listing_terms import selected_policies

APPROVED = {
    'operas-326': ('great-operas.json', 'MW-2026-326', '14.95', '2026-11-18T12:00:00Z'),
    'fowles-325': ('french-lieutenants-woman.json', 'MW-2026-325', '7.95', '2026-11-17T12:00:00Z'),
    'lincoln-324': ('lincolns-sons.json', 'MW-2026-324', '9.95', '2026-11-22T12:00:00Z'),
    'spirits-226': ('wines-spirits.json', 'MW-2026-226', '14.95', '2026-11-18T12:00:00Z'),
    'china-227': ('cooking-china.json', 'MW-2026-227', '19.95', '2026-11-17T12:00:00Z'),
    'mug-321': ('room-essentials-mug.json', 'MW-2026-321', '8.95', '2026-11-17T12:00:00Z'),
    'babbitt-292': ('babbitt.json', 'MW-2026-292', '19.95', '2026-11-18T12:00:00Z'),
    'chile-291': ('green-chile-bible.json', 'MW-2026-291', '4.95', '2026-11-19T12:00:00Z'),
    'betty-323': ('betty-smith-omnibus.json', 'MW-2026-323', '7.95', '2026-11-16T12:00:00Z'),
    'mothers-322': ('mothers-encyclopedia.json', 'MW-2026-322', '19.95', '2026-11-22T12:00:00Z'),
    'wine-290': ('world-atlas-wine.json', 'MW-2026-290', '7.95', '2026-11-21T12:00:00Z'),
    'mcgee-293': ('on-food-and-cooking.json', 'MW-2026-293', '12.95', '2026-11-20T12:00:00Z'),
}
LOCK = threading.Lock()


def load_approval(code):
    if code not in APPROVED:
        raise PublishError('No approved item exists at this address.')
    filename, sku, price, start = APPROVED[code]
    record = json.loads((Path(__file__).parent / 'approved' / filename).read_text())
    item = record['listing']
    sha = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if sha != record['payload_sha256'] or item['sku'] != sku or item['listingItems']['items'][0]['itemTerms']['pricingTerms']['fixedPrice'] != price or item['listingTerms']['lifecycleTerms']['scheduledToStartAt'] != start:
        raise PublishError('The committed item does not match its approved price and publication time.')
    return record


def submit(code, token, http=None, now=None):
    if not LOCK.acquire(blocking=False):
        raise PublishError('An approved submission is running. Wait for its receipt.')
    try:
        record = load_approval(code)
        item = record['listing']
        http = http or requests.Session()
        existing = find_existing(token, item['sku'], http)
        if existing:
            observed = verify(token, existing[0], http)
            check_observed(item, observed)
            return {'created': False, 'sku': item['sku'], 'listing_id': existing[0], 'observed': observed, 'message': 'Existing matching listing verified; no new listing created.'}
        when = datetime.fromisoformat(item['listingTerms']['lifecycleTerms']['scheduledToStartAt'].replace('Z', '+00:00'))
        if when <= (now or datetime.now(timezone.utc)):
            raise PublishError('The approved time has passed. Choose a new day in chat before any submission.')
        policies = selected_policies(token, item, http)
        if not all('calculated shipping paid by buyer' in x for x in policies['shipping'] if not x.startswith('Handling:')) or '30 day' not in policies['returns'] or 'buyer' not in policies['returns']:
            raise PublishError('Shipping or returns differ from the disclosed terms. Review them before submission.')
        checked = validate_listing(token, item, http)
        if not checked['ok']:
            raise PublishError('eBay validation failed. Nothing was submitted. ' + ' '.join(checked['errors']))
        # Reuse this exact committed key and payload. Never generate retry keys.
        operation = post(token, CREATE_QUERY, {'input': item}, http).get('createListing') or {}
        listing_id = (operation.get('listing') or {}).get('listingId')
        if operation.get('__typename') != 'CreateListingSuccess' or not listing_id:
            raise PublishError('eBay did not confirm creation. Check Seller Hub before another attempt; do not replace the saved creation key.')
        result = {'created': True, 'sku': item['sku'], 'listing_id': listing_id, 'observed': None, 'validation': checked, 'policies': policies,
                  'message': 'eBay accepted creation. Current-state verification is pending.'}
        try:
            observed = verify(token, listing_id, http)
            check_observed(item, observed)
        except PublishError:
            return result
        result.update(observed=observed, message='eBay creation accepted and approved details verified.')
        return result
    finally:
        LOCK.release()


def register(app, page, access_token, tokens, configured):
    @app.get('/publish/approved')
    def approved_home():
        records = [(code, load_approval(code)) for code in APPROVED]
        return page('Approved listings', '''
<p>Open an item to check its approved price, publication day and eBay result. Only individually approved items are available here.</p>
<ul>{% for code, record in records %}<li><a href="{{ url_for('publish_approved', code=code) }}">{{ record.inventory_id }} — {{ record.listing.product.title }}</a><br>${{ record.price_disclosed }} · {{ record.publication_label }}</li>{% endfor %}</ul>
<p>Scheduling checks eBay for an existing listing before creating one. Download the receipt after submission; catalog updates are completed separately.</p>''', records=records)

    @app.route('/publish/approved/<code>', methods=['GET', 'POST'])
    def publish_approved(code):
        try:
            approved = load_approval(code)
        except (PublishError, KeyError, OSError, ValueError):
            abort(404, 'Approved listing unavailable.')
        token = access_token() if configured() else None
        entry = tokens.get(session.get('sid'), {})
        authorized = bool(token) and LISTING_SCOPE in entry.get('scope', '').split()
        session.setdefault('approved_csrf', secrets.token_urlsafe(32))
        if request.method == 'POST':
            if not authorized:
                abort(401, 'Connect eBay for listing checks, then return here.')
            if not secrets.compare_digest(session['approved_csrf'], request.form.get('csrf', '')):
                abort(403, 'Reload before submitting.')
            try:
                result = submit(code, token)
            except PublishError as exc:
                return page('Approved listing needs attention', '<p>{{ error }}</p>', error=str(exc)), 409
            entry.setdefault('approved_receipts', {})[code] = result
            life = (result.get('observed') or {}).get('listingTerms', {}).get('lifecycleTerms', {})
            return page('Approved listing result', '''
<p>{{ result.message }}</p><p>Catalog number: {{ result.sku }}</p><p>eBay listing ID: <strong>{{ result.listing_id }}</strong></p>
<p>Observed status: {{ life.listingStatus or 'Verification pending' }}</p><p>Observed start: {{ life.startAt or 'Not returned' }}</p>
<p><a href="https://www.ebay.com/itm/{{ result.listing_id }}">Open eBay listing</a></p>
<p><a href="{{ url_for('approved_receipt', code=code) }}">Download receipt</a></p>
<p>Reconcile this receipt with the master catalog. Do not create another listing for this item.</p>''', result=result, life=life, code=code)
        return page('Schedule approved item', '''
<p><strong>{{ approved.listing.product.title }}</strong></p><p>{{ approved.inventory_id }}</p>
<p>Approved price: <strong>${{ approved.price_disclosed }}</strong> {{ approved.get('shipping_description', 'plus buyer-paid calculated USPS Ground Advantage shipping') }}. Best Offer enabled; automatic acceptance and decline off. 30-day buyer-paid returns.</p>
<p>Approved start: <strong>{{ approved.get('publication_label', 'Tuesday, November 17, 2026 at 7 a.m. Eastern') }}</strong>.</p>
{% if authorized %}<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Schedule the approved item</button></form>
{% else %}<p><a href="{{ url_for('login', validation='1') }}">Connect eBay for listing checks</a>, then return to this page.</p>{% endif %}
<p>This manual flow uses a committed item-specific creation key and eBay duplicate checks. Receipts remain in memory until downloaded; catalog reconciliation is handled in chat.</p>''', approved=approved, authorized=authorized, csrf=session['approved_csrf'])

    @app.get('/publish/approved/<code>/receipt')
    def approved_receipt(code):
        if not access_token():
            abort(401)
        record = tokens.get(session.get('sid'), {}).get('approved_receipts', {}).get(code)
        if not record:
            abort(404)
        return Response(json.dumps(record, indent=2), mimetype='application/json', headers={'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="' + record['sku'] + '_schedule_receipt.json"'})
