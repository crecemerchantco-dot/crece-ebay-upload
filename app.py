import os
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Flask, abort, redirect, render_template_string, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'development-only-change-me')
app.config.update(
    SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    MAX_CONTENT_LENGTH=80 * 1024 * 1024,
)

API = 'https://api.ebay.com'
SCOPE = 'https://api.ebay.com/oauth/api_scope/sell.inventory'
MEDIA_URL = API + '/commerce/media/v1_beta/image/create_image_from_file'
INVENTORY_URL = API + '/sell/inventory/v1/inventory_item?limit=1'
TOKENS = {}  # Temporary: reconnect after a Render restart.

STYLE = '''
body{font:16px system-ui,-apple-system,sans-serif;max-width:750px;margin:25px auto;padding:0 16px;line-height:1.5;color:#222}
section{border:1px solid #ddd;border-radius:12px;padding:18px;margin:18px 0}
button,.button{display:inline-block;background:#1759b0;color:white;border:0;border-radius:8px;padding:12px 17px;font:inherit;text-decoration:none;cursor:pointer}
input{max-width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f4f4;padding:12px;border-radius:8px}
.ok{color:#146c35}.bad{color:#a22323}.muted{color:#555;font-size:14px}
'''


def configured():
    return all(os.environ.get(k) for k in ('EBAY_CLIENT_ID', 'EBAY_CLIENT_SECRET', 'EBAY_RUNAME', 'FLASK_SECRET_KEY'))


def credentials():
    return (os.environ['EBAY_CLIENT_ID'], os.environ['EBAY_CLIENT_SECRET'])


def access_token():
    entry = TOKENS.get(session.get('sid'))
    if not entry:
        return None
    if entry['expires'] > time.time() + 90:
        return entry['access_token']
    if not entry.get('refresh_token'):
        return None
    try:
        response = requests.post(
            API + '/identity/v1/oauth2/token', auth=credentials(),
            data={'grant_type': 'refresh_token', 'refresh_token': entry['refresh_token'], 'scope': SCOPE},
            timeout=25,
        )
        if not response.ok:
            app.logger.warning('eBay token refresh failed: HTTP %s', response.status_code)
            return None
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
    return render_template_string(template, title=title, **context)


@app.get('/')
def index():
    return page('Crece Merchant Co — eBay Photos', '''
<p>Upload listing photographs directly to eBay, or run two tests to diagnose upload errors.</p>
{% if not ready %}<section>Render configuration is incomplete.</section>
{% elif not connected %}<section><a class="button" href="{{ url_for('login') }}">Connect to eBay</a></section>
{% else %}
<section><p class="ok"><strong>Connected to eBay</strong></p>
<h2>Upload listing photographs</h2>
<form action="{{ url_for('upload') }}" method="post" enctype="multipart/form-data">
<label>Choose 1–24 JPEG, PNG or WebP photographs (select the main photo first):<br><input type="file" name="photos" accept="image/jpeg,image/png,image/webp" multiple required></label><p><button type="submit">Upload photographs</button></p></form></section>
<section><h2>Run Diagnostics</h2>
<p><strong>Test A:</strong> Make a read-only request to eBay Inventory API.<br><strong>Test B:</strong> Upload one small JPEG to the same Media API used by the uploader.</p>
<form action="{{ url_for('diagnostics') }}" method="post" enctype="multipart/form-data">
<label>Choose one small JPEG (ideally under 2 MB):<br><input type="file" name="photo" accept="image/jpeg" required></label><p><button type="submit">Run both tests</button></p></form>
<p class="muted">Neither test publishes a listing. Test B does upload your photograph to eBay.</p></section>
<form action="{{ url_for('disconnect') }}" method="post"><button type="submit">Disconnect eBay</button></form>
{% endif %}<p><a href="{{ url_for('privacy') }}">Privacy policy</a></p>
''', ready=configured(), connected=bool(access_token()) if configured() else False)


@app.get('/health')
def health():
    return {'status': 'ok', 'configured': bool(configured())}


@app.get('/privacy')
def privacy():
    return page('Privacy Policy — Crece Merchant Co', '''<p>This private seller application uses eBay OAuth to upload product photographs to eBay. Tokens are held temporarily in server memory, and photographs are transmitted to eBay without being permanently stored by this application. The hosting provider may retain ordinary request logs.</p><p>Contact: crecemerchantco@gmail.com</p>''')


@app.get('/login')
def login():
    if not configured():
        abort(503, 'Missing Render environment variables')
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    params = {'client_id': os.environ['EBAY_CLIENT_ID'], 'response_type': 'code', 'redirect_uri': os.environ['EBAY_RUNAME'], 'scope': SCOPE, 'state': state}
    return redirect('https://auth.ebay.com/oauth2/authorize?' + urlencode(params))


@app.get('/oauth/callback')
def callback():
    expected = session.pop('oauth_state', None)
    received = request.args.get('state', '')
    code = request.args.get('code')
    if not expected or not secrets.compare_digest(expected, received) or not code:
        abort(400, 'Authorization failed or state mismatch')
    try:
        response = requests.post(
            API + '/identity/v1/oauth2/token', auth=credentials(),
            data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': os.environ['EBAY_RUNAME']},
            timeout=25,
        )
        if not response.ok:
            app.logger.error('eBay OAuth exchange failed: HTTP %s', response.status_code)
            abort(502, 'eBay authorization failed')
        data = response.json()
    except (requests.RequestException, ValueError, KeyError):
        app.logger.exception('eBay OAuth exchange failed')
        abort(502, 'Could not complete eBay authorization')
    sid = secrets.token_urlsafe(32)
    session['sid'] = sid
    TOKENS[sid] = {'access_token': data['access_token'], 'refresh_token': data.get('refresh_token'), 'expires': time.time() + data['expires_in']}
    return redirect(url_for('index'))


@app.get('/oauth/declined')
def declined():
    return page('Authorization declined', '<p>You did not authorize the application.</p>')


@app.post('/disconnect')
def disconnect():
    sid = session.pop('sid', None)
    if sid:
        TOKENS.pop(sid, None)
    return redirect(url_for('index'))


def safe_details(response):
    """Expose useful diagnostic information, never auth headers or tokens."""
    details = {'status': response.status_code, 'content_type': response.headers.get('Content-Type', '(not provided)'),
               'request_id': response.headers.get('X-EBAY-C-REQUEST-ID') or response.headers.get('X-EBAY-REQUEST-ID') or '(not provided)',
               'location_present': bool(response.headers.get('Location'))}
    if not response.ok:
        details['response_excerpt'] = response.text[:1200]
    return details


def upload_image(token, photo):
    """Use the current upload request unchanged to isolate the persistent 503."""
    photo.stream.seek(0)
    try:
        response = requests.post(
            MEDIA_URL,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
            files={'image': (photo.filename, photo.stream, photo.mimetype)},
            timeout=(15, 90), allow_redirects=False,
        )
    except requests.RequestException as exc:
        return {'status': 'NETWORK ERROR', 'error': type(exc).__name__, 'url': None}
    result = safe_details(response)
    result['url'] = None
    if response.ok:
        try:
            data = response.json() if response.content else {}
            result['url'] = data.get('imageUrl')
        except ValueError:
            result['error'] = 'Successful HTTP response was not JSON.'
        if not result['url']:
            result['note'] = 'No imageUrl in response. Resource Location header present: ' + str(bool(response.headers.get('Location')))
    return result


@app.post('/diagnostics')
def diagnostics():
    token = access_token()
    if not token:
        return redirect(url_for('login'))
    photo = request.files.get('photo')
    if not photo or not photo.filename or photo.mimetype != 'image/jpeg':
        abort(400, 'Choose one JPEG photograph')

    # Test A: read-only Inventory API call; an empty inventory (200) is fine.
    try:
        response = requests.get(
            INVENTORY_URL,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
            timeout=(15, 30), allow_redirects=False,
        )
        test_a = safe_details(response)
        test_a['passed'] = response.status_code == 200
        if response.status_code == 401:
            test_a['note'] = 'Authorization failed: reconnect your eBay account.'
        elif response.status_code == 403:
            test_a['note'] = 'eBay denied Inventory API access; check account and scopes.'
    except requests.RequestException as exc:
        test_a = {'status': 'NETWORK ERROR', 'error': type(exc).__name__, 'passed': False}

    # Test B: same upload request as normal uploader; no automatic retries.
    test_b = upload_image(token, photo)
    test_b['passed'] = isinstance(test_b['status'], int) and 200 <= test_b['status'] < 300

    return page('eBay Diagnostic Results', '''
<section><h2>Test A — Inventory API</h2><p class="{{ 'ok' if a.passed else 'bad' }}"><strong>{{ 'PASS' if a.passed else 'NEEDS INVESTIGATION' }}</strong></p>
<pre>{{ a | tojson(indent=2) }}</pre></section>
<section><h2>Test B — Media API photograph upload</h2><p class="{{ 'ok' if b.passed else 'bad' }}"><strong>{{ 'PASS' if b.passed else 'NEEDS INVESTIGATION' }}</strong></p>
<pre>{{ b | tojson(indent=2) }}</pre></section>
<p class="muted">You can copy these two results into our chat. Do not send access tokens, credentials, or OAuth callback URLs.</p>
<a class="button" href="{{ url_for('index') }}">Upload another photo</a>
''', a=test_a, b=test_b)


@app.post('/upload')
def upload():
    token = access_token()
    if not token:
        return redirect(url_for('login'))
    photos = [p for p in request.files.getlist('photos') if p.filename]
    if not 1 <= len(photos) <= 24:
        abort(400, 'Choose 1–24 photographs')
    allowed = {'image/jpeg', 'image/png', 'image/webp'}
    results = []
    for photo in photos:
        if photo.mimetype not in allowed:
            results.append({'filename': photo.filename, 'status': 'INVALID FORMAT', 'url': None})
            continue
        result = upload_image(token, photo)
        result['filename'] = photo.filename
        results.append(result)
    return page('eBay Upload Results', '''
<p>The first successfully uploaded photograph should be the main image in your listing.</p>
{% for item in results %}<section><h2>{{ loop.index }}. {{ item.filename }}</h2>
{% if item.url %}<p class="ok">Uploaded successfully</p><p><a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">View photograph</a></p><pre>{{ item.url }}</pre>
{% else %}<p class="bad">Upload not confirmed</p><pre>{{ item | tojson(indent=2) }}</pre>{% endif %}</section>{% endfor %}
<a class="button" href="{{ url_for('index') }}">Upload another photo</a>
''', results=results)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
