import io
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from PIL import Image, ImageOps, UnidentifiedImageError
from flask import Flask, abort, redirect, render_template_string, request, session, url_for, Response

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
                                 data={'grant_type': 'refresh_token', 'refresh_token': entry['refresh_token'], 'scope': SCOPE}, timeout=25)
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
    return render_template_string(template, title=title, **context)


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
{% endif %}<p><a href="{{ url_for('privacy') }}">Privacy policy</a></p>
''', ready=configured(), connected=bool(access_token()) if configured() else False)


@app.get('/health')
def health():
    return {'status': 'ok', 'configured': bool(configured())}


@app.get('/privacy')
def privacy():
    return page('Privacy Policy — Crece Merchant Co', '''<p>This private seller application uses eBay OAuth to upload photographs to eBay. OAuth tokens and temporary photos are held in server memory; photos are temporarily available at unguessable public links so eBay can fetch them. Photos expire from the app after approximately 30 minutes or upon a server restart, whichever comes first. The hosting provider may retain request logs. This app does not publish listings.</p><p>Contact: crecemerchantco@gmail.com</p>''')


@app.get('/login')
def login():
    if not configured():
        abort(503, 'Missing Render environment variables')
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    params = {'client_id': os.environ['EBAY_CLIENT_ID'], 'response_type': 'code',
              'redirect_uri': os.environ['EBAY_RUNAME'], 'scope': SCOPE, 'state': state}
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
                   'expires': time.time() + data['expires_in']}
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
