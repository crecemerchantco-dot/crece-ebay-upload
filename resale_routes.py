"""Owner-only reusable listing pages. Missing setup fails closed."""
import io
import json
import os
import re
import secrets
import time
from functools import wraps
from flask import abort, redirect, request, session, url_for, Response
from werkzeug.security import check_password_hash
from werkzeug.datastructures import FileStorage
from catalog_bridge import Catalog, CatalogError
from listing_store import Journal, StoreError
from listing_terms import selected_policies
from ebay_listing import LISTING_SCOPE, MAX_BUNDLE_BYTES, load_upload
from ebay_publish import PublishError
from resale_workflow import review, select_schedule, run, digest

LOGIN_ATTEMPTS = {}


def setup_ready():
    return all(os.environ.get(k) for k in ('APP_ACCESS_PASSWORD_HASH', 'LISTING_DATABASE_URL', 'GOOGLE_SERVICE_ACCOUNT_JSON'))


def owner_signed_in():
    return bool(os.environ.get('APP_ACCESS_PASSWORD_HASH')) and session.get('owner_until', 0) > time.time()


def register(app, page, access_token, tokens, configured, upload_photo):
    def owner(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not owner_signed_in():
                return redirect(url_for('owner_login'))
            return fn(*args, **kwargs)
        return wrapped

    def csrf():
        session.setdefault('workflow_csrf', secrets.token_urlsafe(32))
        return session['workflow_csrf']

    def check_csrf():
        if not secrets.compare_digest(csrf(), request.form.get('csrf', '')):
            abort(403, 'Reload this page before submitting.')

    def connection():
        token = access_token() if configured() else None
        entry = tokens.get(session.get('sid'), {})
        if not token or LISTING_SCOPE not in entry.get('scope', '').split():
            return None, entry
        return token, entry

    def adapters():
        if not setup_ready():
            abort(503, 'Publishing is disabled until the private catalog connection and saved receipt journal are configured.')
        return Journal(os.environ['LISTING_DATABASE_URL']), Catalog(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

    def result_page(record):
        return page('Listing receipt', '''
<p><strong>{{ record.sku }}</strong></p><p>eBay submission: {{ record.state }}</p>
{% if record.listing_id %}<p><a href="https://www.ebay.com/itm/{{ record.listing_id }}">Open eBay listing {{ record.listing_id }}</a></p>{% endif %}
{% if record.observed %}<p>Observed eBay status: {{ record.observed.listingTerms.lifecycleTerms.listingStatus }}.<br>Start: {{ record.observed.listingTerms.lifecycleTerms.startAt }}.</p>{% endif %}
<p>Catalog: <strong>{{ record.catalog_state }}</strong></p>{% if record.catalog_error %}<p>{{ record.catalog_error }}</p>{% endif %}
<p>The receipt is saved automatically. Do not create another listing for this item.</p>
<form method="post" action="{{ url_for('recover_listing') }}"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="sku" value="{{ record.sku }}"><button>Recheck receipt and retry catalog update</button></form>
<p><a href="{{ url_for('saved_receipt', sku=record.sku, download='1') }}">Download receipt</a></p>
<p><a href="{{ url_for('resale_home') }}">Prepare another item</a></p>''', record=record, csrf=csrf())

    @app.route('/owner/login', methods=['GET', 'POST'])
    def owner_login():
        password_hash = os.environ.get('APP_ACCESS_PASSWORD_HASH')
        error = None
        if request.method == 'POST':
            check_csrf()
            if not password_hash:
                abort(503, 'Private access has not been configured.')
            # Global bounded per-process limit; restarts reset it. No client IP
            # headers are trusted. Strong password still required at setup.
            window = int(time.time() // 900)
            attempts = LOGIN_ATTEMPTS.setdefault(window, 0)
            for old in list(LOGIN_ATTEMPTS):
                if old != window:
                    LOGIN_ATTEMPTS.pop(old, None)
            if attempts >= 10:
                abort(429, 'Too many sign-in attempts. Try again in 15 minutes.')
            LOGIN_ATTEMPTS[window] += 1
            if check_password_hash(password_hash, request.form.get('password', '')):
                session['owner_until'] = time.time() + 8 * 3600
                session['workflow_csrf'] = secrets.token_urlsafe(32)
                return redirect(url_for('resale_home'))
            error = 'Password not recognized.'
        return page('Your resale workspace', '''
{% if not ready %}<p>The repeatable listing flow is built but setup is incomplete. Publishing here is disabled until private access, saved receipts and the catalog connection are ready.</p>
{% else %}<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><label>Workspace password <input type="password" name="password" autocomplete="current-password" required></label><p><button>Open my workspace</button></p></form>{% endif %}
{% if error %}<p>{{ error }}</p>{% endif %}''', ready=bool(password_hash), error=error, csrf=csrf())

    @app.route('/sell', methods=['GET', 'POST'])
    @owner
    def resale_home():
        ready = setup_ready()
        token, entry = connection()
        if request.method == 'POST':
            check_csrf()
            _, catalog = adapters()
            if not token:
                abort(401, 'Connect your eBay seller account first.')
            entry.pop('resale_draft', None)
            entry.pop('resale_review_hash', None)
            upload = request.files.get('package')
            try:
                item, photos = load_upload(upload.read(MAX_BUNDLE_BYTES + 1) if upload else b'')
                catalog.ensure_unlisted(item['sku'])
                if photos:
                    urls = []
                    for name, data in photos:
                        result = upload_photo(token, FileStorage(stream=io.BytesIO(data), filename=name))
                        if not result.get('url'):
                            raise PublishError('Photograph upload did not complete. No listing was submitted.')
                        urls.append(result['url'])
                    item['product']['imageUrls'] = urls
                details = review(item)
                policies = selected_policies(token, item)
                entry['resale_policies'] = policies
                entry['resale_draft'] = item
                entry['resale_review_hash'] = digest(item)
                entry['resale_review_until'] = time.time() + 3600
                # Price appears before any timing choice; no publication action
                # accepts a client-supplied price or a client-supplied payload.
                return page('Price and publication day', '''
<p><strong>{{ details.title }}</strong><br>{{ details.sku }} · {{ details.photo_count }} photographs</p>
<p>Price: <strong>${{ details.price }}</strong></p>
<p>Shipping: {{ policies.names.fulfillment }}</p><ul>{% for line in policies.shipping %}<li>{{ line }}</li>{% endfor %}</ul><p>{{ policies.returns }}</p>
<p>Best Offer: {{ 'enabled' if details.offer.bestOfferEnabled else 'disabled' }}.</p>
<p>Automatic offer acceptance: {% if details.offer.autoAcceptThreshold %}${{ details.offer.autoAcceptThreshold }} or higher{% else %}off{% endif %}.<br>Automatic offer decline: {% if details.offer.autoDeclineThreshold %}${{ details.offer.autoDeclineThreshold }} or lower{% else %}off{% endif %}.</p>
<form method="post" action="{{ url_for('submit_resale') }}"><input type="hidden" name="csrf" value="{{ csrf }}">
<p><label>When should it be published?<br><input type="date" name="day"></label></p>
<p><label>Time in Eastern time <input type="time" name="clock"></label><br>Leave time blank for 7 a.m. Eastern.</p>
<p><button name="action" value="schedule">Schedule on this day</button></p>
<p><button name="action" value="now">Publish now</button></p></form>
<p>The app validates with eBay, saves the submission receipt, then updates your catalog automatically.</p>''', details=details, policies=policies, csrf=csrf())
            except (ValueError, PublishError, CatalogError) as exc:
                return page('Listing needs attention', '<p>{{ error }}</p>', error=str(exc)), 400
        return page('Sell an item', '''
{% if not ready %}<p>Publishing is disabled until saved receipts and the private catalog connection are configured.</p>
{% elif not token %}<p><a class="button" href="{{ url_for('login', publish='resale') }}">Connect my eBay seller account</a></p>
{% else %}<form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{{ csrf }}"><label>Choose the prepared listing package <input type="file" name="package" accept=".zip,.json" required></label><p><button>Upload photos and review price</button></p></form>{% endif %}
<p>Your publication day is required. If you leave the time blank, the app uses 7 a.m. Eastern.</p>
<form method="get" action="{{ url_for('receipt_lookup') }}"><label>Find a saved receipt by catalog number <input name="sku" placeholder="MW-2026-..." required></label><p><button>Find receipt</button></p></form>
<p>Your eBay connection may need renewing after an app restart. Saved receipts remain available.</p>''', ready=ready, token=token, csrf=csrf())

    @app.post('/sell/submit')
    @owner
    def submit_resale():
        check_csrf()
        journal, catalog = adapters()
        token, entry = connection()
        if not token:
            abort(401, 'Reconnect eBay before submitting.')
        item = entry.get('resale_draft')
        if not item or entry.get('resale_review_until', 0) < time.time() or digest(item) != entry.get('resale_review_hash'):
            abort(409, 'Upload the package and review its price again before choosing publication.')
        action = request.form.get('action')
        if action not in ('now', 'schedule'):
            abort(400, 'Choose a publication day or publish now.')
        try:
            policies = selected_policies(token, item)
            if policies['fingerprint'] != entry.get('resale_policies', {}).get('fingerprint'):
                raise PublishError('A selling policy changed after price review. Upload the package and review its shipping terms again.')
            item = select_schedule(item, request.form.get('day', ''), request.form.get('clock', ''), immediate=action == 'now')
            result = run(token, item, journal, catalog, policy_review=policies)
            entry.pop('resale_draft', None)
            return result_page(result)
        except (PublishError, CatalogError, StoreError) as exc:
            return page('Check the saved listing result', '''<p>{{ error }}</p><p>No automatic resubmission will be made. Use the saved receipt to check the outcome.</p><p><a href="{{ url_for('saved_receipt', sku=sku) }}">Check this item’s receipt</a></p>''', error=str(exc), sku=item['sku']), 409

    @app.get('/sell/receipts')
    @owner
    def receipt_lookup():
        return redirect(url_for('saved_receipt', sku=request.args.get('sku', '')))

    @app.get('/sell/receipts/<sku>')
    @owner
    def saved_receipt(sku):
        if not re.fullmatch(r'MW-\d{4}-\d{3}[A-Z]?', sku):
            abort(400, 'Enter the catalog number.')
        journal, _ = adapters()
        try:
            with journal.locked(sku) as entry:
                record = entry.get()
            if not record:
                return page('No saved submission', '<p>No creation request was saved for {{ sku }}. Return to the item workflow to review it.</p>', sku=sku), 404
            if request.args.get('download') == '1':
                return Response(json.dumps(record, indent=2), mimetype='application/json', headers={'Cache-Control': 'no-store', 'Content-Disposition': f'attachment; filename="{sku}_receipt.json"'})
            return result_page(record)
        except StoreError as exc:
            return page('Receipts unavailable', '<p>{{ error }}</p>', error=str(exc)), 503

    @app.post('/sell/recover')
    @owner
    def recover_listing():
        check_csrf()
        journal, catalog = adapters()
        token, _ = connection()
        if not token:
            return redirect(url_for('login', publish='resale'))
        sku = request.form.get('sku', '')
        if not re.fullmatch(r'MW-\d{4}-\d{3}[A-Z]?', sku):
            abort(400)
        try:
            with journal.locked(sku) as entry:
                record = entry.get()
            if not record:
                abort(404)
            return result_page(run(token, record['listing'], journal, catalog, reconcile_only=True))
        except (PublishError, StoreError, CatalogError) as exc:
            return page('Reconciliation needs attention', '<p>{{ error }}</p>', error=str(exc)), 409

