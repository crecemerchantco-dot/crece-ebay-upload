"""Submit only Seller Hub Draft rows through eBay's Sell Feed API."""
import csv
import hashlib
import io
import re
import secrets
from threading import Lock
from urllib.parse import urlsplit

import requests
from flask import Response, abort, redirect, request, session, url_for

TASKS_URL = 'https://api.ebay.com/sell/feed/v1/task'
FILENAME = 'seller-hub-draft.csv'
ACTION_HEADER = '*Action(SiteID=US|Country=US|Currency=USD|Version=1193)'
TASK_ID = re.compile(r'[A-Za-z0-9_-]{1,100}')
COMPLETE = {'COMPLETED', 'COMPLETED_WITH_ERROR', 'FAILED'}
submission_lock = Lock()


class DraftError(ValueError):
    pass


def make_csv(fields):
    title = fields.get('title', '').strip()
    category = fields.get('category', '').strip()
    sku = fields.get('sku', '').strip()
    price = fields.get('price', '').strip()
    condition = fields.get('condition', '')
    description = fields.get('description', '').strip().replace('\r\n', '\n')
    photos = [url.strip() for url in fields.get('photos', '').splitlines() if url.strip()]
    if not title or len(title) > 80 or title.startswith(('=', '+', '-', '@')):
        raise DraftError('Enter a listing title of 1–80 characters.')
    if not re.fullmatch(r'[0-9]{1,10}', category):
        raise DraftError('Enter a numeric eBay category ID.')
    if sku and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', sku):
        raise DraftError('Enter a valid SKU or leave it blank.')
    if not re.fullmatch(r'[0-9]{1,10}(\.[0-9]{1,2})?', price) or not float(price):
        raise DraftError('Enter a positive price in US dollars.')
    if condition not in {'NEW', 'USED'}:
        raise DraftError('Choose New or Used.')
    if len(description) > 40000 or description.startswith(('=', '+', '-', '@')):
        raise DraftError('Enter a description under 40,000 characters.')
    if len(photos) > 24 or len('|'.join(photos)) > 2048 or any(
        not re.fullmatch(r'https://[^\s|]+', url) for url in photos
    ):
        raise DraftError('Enter up to 24 HTTPS photo URLs, one per line, up to 2,048 characters total.')
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow([ACTION_HEADER, 'Custom label (SKU)', 'Category ID', 'Title', 'UPC',
                     'Price', 'Quantity', 'Item photo URL', 'Condition ID', 'Description', 'Format'])
    writer.writerow(['Draft', sku, category, title, '', price, '1', '|'.join(photos),
                     condition, '<br>'.join(description.splitlines()), 'FixedPrice'])
    return output.getvalue().encode('utf-8')


def feed_request(method, token, path='', **kwargs):
    headers = {'Authorization': f'Bearer {token}', 'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'}
    try:
        response = requests.request(method, TASKS_URL + path, headers=headers, timeout=10,
                                    allow_redirects=False, **kwargs)
    except requests.RequestException as error:
        raise DraftError('eBay did not respond. Check the task before submitting again.') from error
    if not 200 <= response.status_code < 300:
        try:
            messages = [str(error.get('message', '')) for error in response.json().get('errors', [])]
        except (AttributeError, ValueError):
            messages = []
        raise DraftError(f'eBay returned HTTP {response.status_code}. ' + ' '.join(messages)[:500])
    return response


def submit_draft(token, payload, submissions):
    digest = hashlib.sha256(payload).hexdigest()
    with submission_lock:
        if digest in submissions:
            return submissions[digest], None
        response = feed_request('POST', token, json={'feedType': 'FX_LISTING', 'schemaVersion': '1.0'})
        task_id = urlsplit(response.headers.get('Location', '')).path.rstrip('/').rsplit('/', 1)[-1]
        if not TASK_ID.fullmatch(task_id):
            raise DraftError('eBay did not return a usable draft task ID.')
        submissions[digest] = task_id
    try:
        feed_request('POST', token, f'/{task_id}/upload_file',
                     data={'fileName': FILENAME, 'name': 'file', 'type': 'form-data'},
                     files={'file': (FILENAME, payload, 'text/csv')})
    except DraftError as error:
        return task_id, str(error)
    return task_id, None


FORM = """
<p>Send one fixed-price item to Seller Hub Drafts for editing.</p>
{% if error %}<p class="bad">{{ error }}</p>{% endif %}
<form method="post">
<input type="hidden" name="csrf" value="{{ csrf }}">
<p><label>Title <input name="title" maxlength="80" required value="{{ fields.get('title', '') }}"></label></p>
<p><label>eBay category ID <input name="category" inputmode="numeric" required value="{{ fields.get('category', '') }}"></label></p>
<p><label>SKU (optional) <input name="sku" value="{{ fields.get('sku', '') }}"></label></p>
<p><label>Price (USD) <input name="price" inputmode="decimal" required value="{{ fields.get('price', '') }}"></label></p>
<p><label>Condition <select name="condition"><option value="USED">Used</option>
<option value="NEW" {% if fields.get('condition') == 'NEW' %}selected{% endif %}>New</option></select></label></p>
<p><label>Description<br><textarea name="description" rows="7" cols="55" maxlength="40000">{{ fields.get('description', '') }}</textarea></label></p>
<p><label>Photo URLs, one per line, main photo first<br><textarea name="photos" rows="5" cols="55">{{ fields.get('photos', '') }}</textarea></label></p>
{% if connected %}<button name="action" value="submit">Save to eBay Drafts</button>
{% else %}<p><a href="{{ url_for('login', drafts=1) }}">Connect to eBay for drafts</a></p>{% endif %}
<button name="action" value="download">Download draft CSV</button>
</form>
<p>Review fit, shipping and return policies in Seller Hub before listing.</p>
"""


STATUS = """
<p>eBay task {{ task_id }}</p>
{% if error %}<p class="bad">{{ error }}</p>{% endif %}
{% if saved %}<p class="ok">eBay processed one draft successfully.</p>
{% elif status %}<p>Status: {{ status }}. Drafts processed: {{ succeeded }}. Failed rows: {{ failed }}.</p>{% endif %}
<p><a href="{{ url_for('draft_status', task_id=task_id) }}">Refresh task status</a></p>
{% if complete %}<p><a href="{{ url_for('draft_result', task_id=task_id) }}">Download eBay result</a></p>{% endif %}
<p><a href="https://www.ebay.com/sh/lst/drafts">Open Seller Hub Drafts</a></p>
"""


def register(app, page, access_token, tokens, configured):
    @app.route('/drafts', methods=['GET', 'POST'])
    def draft_form():
        token = access_token() if configured() else None
        csrf = session.setdefault('draft_csrf', secrets.token_urlsafe(32))
        error = None
        if request.method == 'POST':
            if not secrets.compare_digest(csrf.encode(), request.form.get('csrf', '').encode()):
                abort(400, 'Reload the draft form and try again.')
            try:
                payload = make_csv(request.form)
                if request.form.get('action') == 'download':
                    return Response(payload, mimetype='text/csv', headers={
                        'Content-Disposition': f'attachment; filename="{FILENAME}"',
                        'Cache-Control': 'no-store',
                    })
                if request.form.get('action') != 'submit' or not token:
                    abort(403)
                entry = tokens.get(session.get('sid'))
                if entry is None:
                    abort(403)
                task_id, upload_error = submit_draft(token, payload, entry.setdefault('draft_tasks', {}))
                if upload_error:
                    return page('Check eBay draft status', STATUS, task_id=task_id, error=upload_error)
                return redirect(url_for('draft_status', task_id=task_id), code=303)
            except DraftError as exc:
                error = str(exc)
        return page('Save to eBay Drafts', FORM, fields=request.form, csrf=csrf,
                    connected=bool(token), error=error)

    @app.get('/drafts/status/<task_id>')
    def draft_status(task_id):
        token = access_token() if configured() else None
        if not token:
            return redirect(url_for('draft_form'))
        if not TASK_ID.fullmatch(task_id):
            abort(404)
        try:
            result = feed_request('GET', token, f'/{task_id}').json()
            if result.get('feedType') != 'FX_LISTING':
                abort(404)
            status = result.get('status')
            summary = result.get('uploadSummary') or {}
            succeeded = summary.get('successCount', 0)
            failed = summary.get('failureCount', 0)
        except ValueError as error:
            return page('Check eBay draft status', STATUS, task_id=task_id, error=str(error))
        owned_tasks = tokens.get(session.get('sid'), {}).get('draft_tasks', {})
        saved = task_id in owned_tasks.values() and status == 'COMPLETED' and succeeded == 1 and failed == 0
        return page('eBay draft status', STATUS, task_id=task_id, status=status, saved=saved,
                    succeeded=succeeded, failed=failed, complete=status in COMPLETE)

    @app.get('/drafts/status/<task_id>/result')
    def draft_result(task_id):
        token = access_token() if configured() else None
        if not token or not TASK_ID.fullmatch(task_id):
            abort(404)
        try:
            response = feed_request('GET', token, f'/{task_id}/download_result_file')
        except DraftError as error:
            return page('Check eBay draft status', STATUS, task_id=task_id, error=str(error))
        extension = 'zip' if response.content.startswith(b'PK') else 'csv'
        return Response(response.content, mimetype='application/octet-stream', headers={
            'Content-Disposition': f'attachment; filename="ebay-draft-result.{extension}"',
            'Cache-Control': 'no-store',
        })
