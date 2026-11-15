import io
import json
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from PIL import Image, ImageOps, UnidentifiedImageError
from flask import Flask, abort, redirect, render_template_string, request, session, url_for, Response
from werkzeug.datastructures import FileStorage
from ebay_checks import ACCOUNT_SCOPE, BASE_SCOPE, CheckError, EbayChecks, preflight
from ebay_listing import LISTING_SCOPE, MAX_BUNDLE_BYTES, load_upload, validate_listing
from ebay_publish import PublishError, approval, schedule_approved_book

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'development-only-change-me')
app.config.update(SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  MAX_CONTENT_LENGTH=80 * 1024 * 1024)

API = 'https://api.ebay.com'  # OAuth and Inventory API
MEDIA_API = 'https://apim.ebay.com'  # Verified working URL-upload host
SCOPE = 'https://api.ebay.com/oauth/api_scope/sell.inventory'
MEDIA_URL = MEDIA_API + '/commerce/media/v1_beta/image/create_image_from_url'
TOKENS = {}  # Reconnect after a Render restart.
APP_TOKEN = {}  # Application-only category metadata; never returned to clients.
TEMP_IMAGES = {}  # Private-to-app memory; public only through unpredictable links, expires after 30 min.
TEMP_TTL = 30 * 60
MAX_PHOTOS = 24

STYLE = '''body{font:16px system-ui,-apple-system,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;line-height:1.5;color:#222}
section{border:1px solid #ddd;border-radius:12px;padding:18px;margin:18px 0}button,.button{display:inline-block;background:#1759b0;color:white;border:0;border-radius:8px;padding:12px 17px;font:inherit;text-decoration:none;cursor:pointer}
input{max-width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f4f4;padding:12px;border-radius:8px}.ok{color:#146c35}.bad{color:#a22323}.muted{color:#555;font-size:14px}img{max-width:200px;height:auto}'''


def configured():
    return all(os.environ.get(k) for k in ('EBAY_CLIENT_ID', 'EBAY_CLIENT_SECRET', 'EBAY_RUNAME', 'FLASK_SECRET_KEY'))


def credentials():
    return os.environ['EBAY_CLIENT_ID'], os.environ['EBAY_CLIENT_SECRET']


def access_token():
    entry = TOKENS.get(session.get('sid'))
    if not entry:
        return None
    if entry['expires'] > time.time() + 90:
        return entry['access_token']
    if not entry.get('refresh_token'):
        return None
    try:
        response = requests.post(API + '/identity/v1/oauth2/token', auth=credentials(),
                                 data={'grant_type': 'refresh_token', 'refresh_token': entry['refresh_token']}, timeout=25)
        response.raise_for_status()
        data = response.json()
        entry.update(access_token=data['access_token'], expires=time.time() + data['expires_in'])
        if data.get('refresh_token'):
            entry['refresh_token'] = data['refresh_token']
        return entry['access_token']
    except (requests.RequestException, ValueError, KeyError):
        app.logger.exception('eBay token refresh failed')
        return None


def page(title, content, **context):
    template = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}</title><style>''' + STYLE + '''</style></head><body><h1>{{ title }}</h1>''' + content + '''<p><a href="{{ url_for('index') }}">Return to uploader</a></p></body></html>'''
    response = Response(render_template_string(template, title=title, **context))
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
def index():
    return page('Crece Merchant Co — eBay Photos', '''
<p>Choose your listing photographs. This app converts them to JPEG, temporarily serves them to eBay, and returns eBay-hosted photo URLs. No Cloudinary step.</p>
{% if not ready %}<section>Render configuration is incomplete.</section>
{% elif not connected %}<section><a class="button" href="{{ url_for('login') }}">Connect to eBay</a></section>
{% else %}<section><p class="ok"><strong>Connected to eBay</strong></p>
<form action="{{ url_for('upload') }}" method="post" enctype="multipart/form-data">
<label>Choose 1–24 photos (choose the main photo first):<br><input type="file" name="photos" accept="image/*,.heic,.heif" multiple required></label>
<p><button type="submit">Upload photographs to eBay</button></p></form>
<p class="muted">Allow time for large batches. Images are temporarily accessible through hard-to-guess links so eBay can retrieve them. This does not publish a listing.</p></section>
<form action="{{ url_for('disconnect') }}" method="post"><button type="submit">Disconnect eBay</button></form>
{% endif %}<p><a href="{{ url_for('checks') }}">Check listing connection</a></p>
<p><a href="{{ url_for('listing_validation') }}">Check a prepared listing with eBay</a></p>
<p><a href="{{ url_for('privacy') }}">Privacy policy</a></p>
''', ready=configured(), connected=bool(access_token()) if configured() else False)


@app.get('/health')
def health():
    return {'status': 'ok', 'configured': bool(configured())}


@app.get('/privacy')
def privacy():
    return page('Privacy Policy — Crece Merchant Co', '''<p>This private seller application uses eBay OAuth to upload photographs to eBay. If you enable listing connection checks, it also reads your shipping, return and payment policy names and identifiers. Category requirements are retrieved using application authorization. Local intake checks stay within this app. If you enable listing checks and upload a prepared listing package, its product and selling details are sent to eBay in validation-only mode. This requires eBay listing permission, but this version does not send publishing requests.</p><p>OAuth tokens and temporary photos are held in server memory; photos are temporarily available at unguessable public links so eBay can fetch them. Photos expire from the app after approximately 30 minutes or upon a server restart, whichever comes first. The hosting provider may retain request logs. Listing checks do not publish. A separate scheduling page can create the specifically approved Kitcho book listing only after an authenticated seller submits that action. Its approved product details and creation key are stored in the application repository; seller tokens remain in process memory. Scheduling results can be downloaded by the seller for catalog reconciliation.</p><p>Contact: crecemerchantco@gmail.com</p>''')


@app.get('/login')
def login():
    if not configured():
        abort(503, 'Missing Render environment variables')
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    scope = SCOPE
    publish = request.args.get('publish') == 'kitcho'
    if request.args.get('checks') == '1' or request.args.get('validation') == '1' or publish:
        scope += ' ' + ACCOUNT_SCOPE
    if request.args.get('validation') == '1' or publish:
        scope += ' ' + LISTING_SCOPE
        session['oauth_destination'] = 'publish_kitcho' if publish else 'listing_validation'
    else:
        session.pop('oauth_destination', None)
    session['requested_scope'] = scope
    params = {'client_id': os.environ['EBAY_CLIENT_ID'], 'response_type': 'code',
              'redirect_uri': os.environ['EBAY_RUNAME'], 'scope': scope, 'state': state}
    return redirect('https://auth.ebay.com/oauth2/authorize?' + urlencode(params))


@app.get('/oauth/callback')
def callback():
    expected = session.pop('oauth_state', None)
    received = request.args.get('state', '')
    code = request.args.get('code')
    if not expected or not secrets.compare_digest(expected, received) or not code:
        abort(400, 'Authorization failed or state mismatch')
    try:
        response = requests.post(API + '/identity/v1/oauth2/token', auth=credentials(),
                                 data={'grant_type': 'authorization_code', 'code': code,
                                       'redirect_uri': os.environ['EBAY_RUNAME']}, timeout=25)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError, KeyError):
        app.logger.exception('eBay OAuth exchange failed')
        abort(502, 'Could not complete eBay authorization')
    sid = secrets.token_urlsafe(32)
    session['sid'] = sid
    TOKENS[sid] = {'access_token': data['access_token'], 'refresh_token': data.get('refresh_token'),
                   'expires': time.time() + data['expires_in'],
                   'scope': data.get('scope', session.pop('requested_scope', SCOPE))}
    return redirect(url_for(session.pop('oauth_destination', 'index')))


@app.route('/validate', methods=['GET', 'POST'])
def listing_validation():
    token = access_token() if configured() else None
    entry = TOKENS.get(session.get('sid'), {})
    authorized = bool(token) and LISTING_SCOPE in entry.get('scope', '').split()
    if request.method == 'POST':
        if not authorized:
            abort(401, 'Connect eBay for listing checks first.')
        expected = session.get('validation_csrf', '')
        if not expected or not secrets.compare_digest(expected, request.form.get('csrf', '')):
            abort(403, 'Reload the listing check page and try again.')
        upload = request.files.get('package')
        try:
            prepared, photos = load_upload(upload.read(MAX_BUNDLE_BYTES + 1) if upload else b'')
        except ValueError as exc:
            return page('Listing package needs attention', '<p>{{ error }}</p><p><a href="{{ url_for(\'listing_validation\') }}">Choose another package</a></p>', error=str(exc)), 400
        entry.pop('prepared_listing', None)
        if photos:
            urls = []
            for name, data in photos:
                photo_result = upload_image_from_url(token, FileStorage(stream=io.BytesIO(data), filename=name))
                if not photo_result.get('url'):
                    return page('Photograph upload needs attention', '<p>eBay did not accept photograph {{ number }}. No listing validation or publishing request was sent.</p><p><a href="{{ url_for(\'listing_validation\') }}">Try the package again</a></p>', number=len(urls) + 1), 502
                urls.append(photo_result['url'])
            prepared['product']['imageUrls'] = urls
        entry['prepared_listing'] = prepared
        result = validate_listing(token, prepared)
        return page('eBay listing check', '''
<p><strong>{{ title_text }}</strong></p><p>Catalog number: {{ sku }}</p>
<p class="{{ 'ok' if result.ok else 'bad' }}"><strong>{{ 'eBay validation passed' if result.ok else 'Listing needs attention' }}</strong></p>
{% for error in result.errors %}<p>{{ error }}</p>{% endfor %}
{% for warning in result.warnings %}<p>{{ warning }}</p>{% endfor %}
<p>This check did not publish or schedule a listing. Passing this check does not reserve a listing or guarantee a later submission.</p>
<p>{{ photo_count }} eBay-hosted photographs attached.</p>
<p><a href="{{ url_for('download_checked_package') }}">Download listing details and photo links</a></p>
<p><a href="{{ url_for('listing_validation') }}">Check another listing</a></p>
''', title_text=prepared['product']['title'], sku=prepared['sku'], result=result, photo_count=len(prepared['product'].get('imageUrls', [])))
    session.setdefault('validation_csrf', secrets.token_urlsafe(32))
    return page('Check a prepared listing', '''
<p>Upload the listing package prepared for your item. eBay will check its details without putting it up for sale.</p>
{% if not ready %}<p>Application configuration is incomplete.</p>
{% elif not authorized %}
<p>eBay requires listing access even for this check. This version of the app sends validation requests only.</p>
<p><a class="button" href="{{ url_for('login', validation='1') }}">Connect eBay for listing checks</a></p>
{% else %}<form method="post" enctype="multipart/form-data">
<input type="hidden" name="csrf" value="{{ csrf }}">
<label>Listing package <input type="file" name="package" accept=".zip,.json,application/zip,application/json" required></label>
<p>A package can include your photographs. They will be uploaded to eBay automatically before the check. Allow time for the upload to finish.</p>
<p><button type="submit">Check with eBay</button></p></form>{% endif %}
<p>No listing will be published or scheduled. Your seller connection may need to be renewed after an app restart.</p>
''', ready=configured(), authorized=authorized, csrf=session['validation_csrf'])


@app.get('/validate/package')
def download_checked_package():
    if not access_token():
        abort(401)
    prepared = TOKENS.get(session.get('sid'), {}).get('prepared_listing')
    if not prepared:
        abort(404)
    return Response(json.dumps({'format': 'crece-ebay-listing-v1', 'listing': prepared}, indent=2),
                    mimetype='application/json', headers={'Cache-Control': 'no-store',
                    'Content-Disposition': 'attachment; filename="' + prepared['sku'] + '_checked.json"'})


@app.route('/publish/kitcho', methods=['GET', 'POST'])
def publish_kitcho():
    token = access_token() if configured() else None
    entry = TOKENS.get(session.get('sid'), {})
    authorized = bool(token) and LISTING_SCOPE in entry.get('scope', '').split()
    session.setdefault('publish_csrf', secrets.token_urlsafe(32))
    try:
        record = approval()
    except (PublishError, ValueError, OSError):
        abort(503, 'The approved book listing is unavailable.')
    if request.method == 'POST':
        if not authorized:
            abort(401, 'Connect your eBay seller account first.')
        if not secrets.compare_digest(session['publish_csrf'], request.form.get('csrf', '')):
            abort(403, 'Reload this page before submitting.')
        try:
            result = schedule_approved_book(token)
        except PublishError as exc:
            return page('Book scheduling needs attention', '<p>{{ error }}</p><p><a href="{{ url_for(\'publish_kitcho\') }}">Return to book scheduling</a></p>', error=str(exc)), 502
        entry['publication_receipt'] = result
        observed = result.get('observed') or {}
        lifecycle = (observed.get('listingTerms') or {}).get('lifecycleTerms') or {}
        return page('Kitcho listing result', '''
<p>{{ result.message }}</p>
<p><strong>eBay listing ID: {{ result.listing_id }}</strong></p>
<p>Observed status: {{ status or 'Verification pending' }}</p>
<p>Observed start time from eBay: {{ start or 'Not returned' }}</p>
<p><a href="https://www.ebay.com/itm/{{ result.listing_id }}">Open eBay listing</a> (a scheduled listing may not be public yet)</p>
<p><a href="{{ url_for('publication_receipt') }}">Download scheduling receipt</a></p>
<p>Send the receipt back to our chat so the catalog can be reconciled. Do not create another listing for this book.</p>
''', result=result, status=lifecycle.get('listingStatus'), start=lifecycle.get('startAt'))
    return page('Schedule your Kitcho book', '''
<p><strong>MW-2026-296 — Kitcho: Japan’s Ultimate Dining Experience</strong></p>
<p>Price: <strong>$29.95</strong> plus calculated Media Mail shipping. Best Offer enabled. Returns within 30 days; buyer pays return shipping.</p>
<p>Approved publication: <strong>Monday, November 16, 2026 at 7 a.m. Eastern</strong>.</p>
<p>This submits the listing to eBay now with a future start time. eBay holds the schedule; your phone does not need to remain open.</p>
{% if not authorized %}<p><a class="button" href="{{ url_for('login', publish='kitcho') }}">Connect eBay to schedule this book</a></p>
{% else %}<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<button type="submit">Schedule for November 16 at 7 a.m.</button></form>{% endif %}
<p>The app checks for an existing listing and validates the approved details before creating anything. This test is limited to this book, price and publication date.</p>
''', authorized=authorized, csrf=session['publish_csrf'])


@app.get('/publish/kitcho/receipt')
def publication_receipt():
    if not access_token():
        abort(401)
    result = TOKENS.get(session.get('sid'), {}).get('publication_receipt')
    if not result:
        abort(404)
    return Response(json.dumps(result, indent=2), mimetype='application/json',
                    headers={'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="MW-2026-296_schedule_receipt.json"'})


def application_token():
    if APP_TOKEN.get('expires', 0) > time.time() + 90:
        return APP_TOKEN['token']
    try:
        response = requests.post(API + '/identity/v1/oauth2/token', auth=credentials(),
                                 data={'grant_type': 'client_credentials', 'scope': BASE_SCOPE},
                                 timeout=(5, 20), allow_redirects=False)
        if response.status_code != 200:
            raise CheckError(response.status_code)
        data = response.json()
        token = data['access_token']
        if not isinstance(token, str) or not token:
            raise ValueError()
        APP_TOKEN.update(token=token, expires=time.time() + int(data['expires_in']))
        return token
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise CheckError() from exc


@app.get('/checks')
def checks():
    return page('Listing connection checks', '''
<p>Check your saved policies and the current category requirements before preparing a listing.</p>
<p>This first stage does not create or schedule listings.</p>
{% if not ready %}<p>Application configuration is incomplete.</p>
{% elif not connected %}<p><a class="button" href="{{ url_for('login', checks='1') }}">Connect eBay for checks</a></p>
{% else %}<p><a class="button" href="{{ url_for('check_policies') }}">Read my shipping and return policies</a></p>
<form action="{{ url_for('check_category') }}" method="get">
<label>eBay category number <input name="category_id" required inputmode="numeric" pattern="[0-9]+"></label>
<button type="submit">Check category requirements</button></form>
<p><a href="{{ url_for('login', checks='1') }}">Reconnect if eBay reports missing permissions</a></p>
{% endif %}''', ready=configured(), connected=bool(access_token()) if configured() else False)


def check_token():
    token = access_token()
    if not token:
        abort(401, 'Connect your eBay account to use listing checks.')
    return token


@app.get('/checks/policies')
def check_policies():
    result = EbayChecks(check_token()).policies()
    return page('Your eBay listing policies', '''
{% for kind, result in policies.items() %}<section><h2>{{ kind|capitalize }}</h2>
{% if result.ok %}<ul>{% for p in result.policies %}<li>{{ p.name }} — {{ p.id }}</li>{% else %}<li>No policies returned.</li>{% endfor %}</ul>
{% else %}<p>{{ result.error }}</p>{% endif %}</section>{% endfor %}
<p>These checks have not changed your account or listings.</p>''', policies=result)


@app.get('/checks/category')
def check_category():
    token = check_token()
    try:
        result = EbayChecks(token, application_token()).category(request.args.get('category_id', ''))
    except ValueError as exc:
        abort(400, str(exc))
    except CheckError as exc:
        return page('Category check unavailable', '<p>{{ error }}</p>', error=str(exc)), 502
    session.setdefault('preflight_csrf', secrets.token_urlsafe(32))
    return page('eBay category requirements', '''
<p>Category {{ result.categoryId }}. Requirements read directly from eBay.</p>
<ul>{% for a in result.aspects %}<li>{{ a.localizedAspectName }} — {% if a.aspectConstraint.aspectRequired %}required{% else %}recommended or optional{% endif %}</li>{% else %}<li>No item specifics returned.</li>{% endfor %}</ul>
<p>Developer check: paste a prepared item JSON record below to check basic fields and required specifics. This is local preflight, not eBay listing acceptance.</p>
<form method="post" action="{{ url_for('check_preflight') }}">
<input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="category_id" value="{{ result.categoryId }}">
<label>Prepared item JSON <textarea name="item" rows="12" style="width:100%" required></textarea></label>
<button type="submit">Run local preflight</button></form>''', result=result, csrf=session['preflight_csrf'])


@app.post('/checks/preflight')
def check_preflight():
    token = check_token()
    expected = session.get('preflight_csrf', '')
    if not expected or not secrets.compare_digest(expected, request.form.get('csrf', '')):
        abort(403, 'Reload category requirements before running a check.')
    try:
        item = json.loads(request.form.get('item', ''))
        category = EbayChecks(token, application_token()).category(request.form.get('category_id', ''))
        result = preflight(item, category)
    except (ValueError, TypeError) as exc:
        return page('Invalid preflight input', '<p>{{ error }}</p>', error=str(exc)), 400
    except CheckError as exc:
        return page('Preflight unavailable', '<p>{{ error }}</p>', error=str(exc)), 502
    return page('Local preflight results', '''
<p>{% if result.ok %}Basic checks passed. eBay validation is still required.{% else %}Correct the following fields.{% endif %}</p>
<ul>{% for error in result.errors %}<li>{{ error }}</li>{% endfor %}</ul>
<ul>{% for warning in result.warnings %}<li>{{ warning }}</li>{% endfor %}</ul>
<p>No listing was created or scheduled.</p>''', result=result)


@app.get('/oauth/declined')
def declined():
    return page('Authorization declined', '<p>You did not authorize the application.</p>')


@app.post('/disconnect')
def disconnect():
    sid = session.pop('sid', None)
    if sid:
        TOKENS.pop(sid, None)
    return redirect(url_for('index'))


def clean_expired():
    now = time.time()
    for key, value in list(TEMP_IMAGES.items()):
        if value['expires'] < now:
            TEMP_IMAGES.pop(key, None)


def public_base_url():
    # PUBLIC_BASE_URL can be set to https://crece-ebay-upload.onrender.com in Render.
    base = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
    if base:
        if not base.startswith('https://'):
            raise ValueError('PUBLIC_BASE_URL must begin with https://')
        return base
    # Fixed known deployment host, not untrusted incoming Host header.
    return 'https://crece-ebay-upload.onrender.com'


def jpeg_bytes(photo):
    # Pillow supports common JPEG, PNG, WebP. HEIC/HEIF require an additional decoder.
    raw = photo.read()
    if not raw:
        raise ValueError('The selected file is empty.')
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            if im.mode in ('RGBA', 'LA') or 'transparency' in im.info:
                rgba = im.convert('RGBA')
                background = Image.new('RGB', rgba.size, 'white')
                background.paste(rgba, mask=rgba.getchannel('A'))
                im = background
            else:
                im = im.convert('RGB')
            output = io.BytesIO()
            im.save(output, 'JPEG', quality=88, optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError('Unsupported photo. Try exporting this image as JPEG or PNG.') from exc


@app.get('/temporary-photo/<photo_id>.jpg')
def temporary_photo(photo_id):
    entry = TEMP_IMAGES.get(photo_id)
    if not entry or entry['expires'] < time.time():
        abort(404)
    return Response(entry['bytes'], mimetype='image/jpeg', headers={
        'Cache-Control': 'public, max-age=1800', 'X-Content-Type-Options': 'nosniff'})


def upload_image_from_url(token, photo):
    try:
        data = jpeg_bytes(photo)
    except ValueError as exc:
        return {'status': 'INVALID IMAGE', 'error': str(exc), 'url': None}
    clean_expired()
    photo_id = secrets.token_urlsafe(32)
    TEMP_IMAGES[photo_id] = {'bytes': data, 'expires': time.time() + TEMP_TTL}
    source_url = public_base_url() + url_for('temporary_photo', photo_id=photo_id)
    try:
        response = requests.post(MEDIA_URL,
                                 headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                                          'Content-Type': 'application/json'},
                                 json={'imageUrl': source_url}, timeout=(15, 120))
        result = {'status': response.status_code, 'url': None,
                  'location_present': bool(response.headers.get('Location'))}
        if response.status_code == 201:
            payload = response.json()
            result['url'] = payload.get('maxDimensionImageUrl') or payload.get('imageUrl')
            result['standard_url'] = payload.get('imageUrl')
            result['expiration_date'] = payload.get('expirationDate')
            if not result['url']:
                result['error'] = 'eBay returned 201 but no image URL.'
        else:
            try:
                payload = response.json()
                result['error'] = payload.get('errors', payload)
            except ValueError:
                result['error'] = response.text[:800] or '(empty response)'
        return result
    except (requests.RequestException, ValueError) as exc:
        return {'status': 'UPLOAD ERROR', 'error': type(exc).__name__, 'url': None}
    # Keep temporary image available for eBay to finish fetching until TTL.


@app.post('/upload')
def upload():
    token = access_token()
    if not token:
        return redirect(url_for('login'))
    photos = [p for p in request.files.getlist('photos') if p.filename]
    if not 1 <= len(photos) <= MAX_PHOTOS:
        abort(400, 'Choose 1–24 photographs')
    results = []
    for photo in photos:
        result = upload_image_from_url(token, photo)
        result['filename'] = photo.filename
        results.append(result)
    urls = [r['url'] for r in results if r.get('url')]
    return page('eBay Photo Results', '''
<p><strong>{{ urls|length }} of {{ results|length }} photographs uploaded successfully.</strong> Keep the main photo first when adding these URLs to your listing.</p>
{% if urls %}<section><h2>Copy all eBay image URLs</h2><textarea rows="{{ [urls|length + 1, 12]|min }}" style="width:100%" readonly>{{ urls|join('\\n') }}</textarea></section>{% endif %}
{% for item in results %}<section><h2>{{ loop.index }}. {{ item.filename }}</h2>
{% if item.url %}<p class="ok">Uploaded to eBay</p><p><img src="{{ item.url }}" alt="Uploaded photo preview"></p><p><a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">Open eBay photograph</a></p><pre>{{ item.url }}</pre>
{% else %}<p class="bad">Upload not confirmed</p><pre>{{ item | tojson(indent=2) }}</pre>{% endif %}</section>{% endfor %}
<a class="button" href="{{ url_for('index') }}">Upload more photos</a>
''', results=results, urls=urls)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
