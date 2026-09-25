
import os
import secrets
import time
import html
import logging
from urllib.parse import urlencode, urlparse

import requests

from flask import (
    Flask,
    request,
    session,
    redirect,
    url_for,
    render_template_string,
    Response,
    abort,
)

# ============================================================
# CRECE MERCHANT CO — EBAY PHOTO UPLOADER
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "development-only-change-this"
)

app.config.update(
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=80 * 1024 * 1024,
)

logging.basicConfig(level=logging.INFO)

# NOTE:
# Tokens are stored in memory. Render restarts will require
# you to connect to eBay again.
TOKENS = {}

EBAY_API = "https://api.ebay.com"

EBAY_SCOPE = (
    "https://api.ebay.com/oauth/api_scope/sell.inventory"
)

UPLOAD_ENDPOINT = (
    EBAY_API
    + "/commerce/media/v1_beta/image/create_image_from_file"
)

# Retry only temporary server failures.
RETRY_STATUS_CODES = {502, 503, 504}

# Limit the number of attempts so a broken eBay service
# does not leave the uploader running indefinitely.
MAX_ATTEMPTS = 2


# ============================================================
# CONFIGURATION
# ============================================================

def config_ready():
    required = [
        "EBAY_CLIENT_ID",
        "EBAY_CLIENT_SECRET",
        "EBAY_RUNAME",
        "FLASK_SECRET_KEY",
    ]

    return all(os.environ.get(key) for key in required)


def ebay_credentials():
    return (
        os.environ["EBAY_CLIENT_ID"],
        os.environ["EBAY_CLIENT_SECRET"],
    )


# ============================================================
# OAUTH TOKEN MANAGEMENT
# ============================================================

def get_access_token():
    sid = session.get("sid")

    if not sid:
        return None

    entry = TOKENS.get(sid)

    if not entry:
        return None

    if entry["expires"] > time.time() + 90:
        return entry["access_token"]

    refresh_token = entry.get("refresh_token")

    if not refresh_token:
        return None

    try:
        response = requests.post(
            EBAY_API + "/identity/v1/oauth2/token",
            auth=ebay_credentials(),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": EBAY_SCOPE,
            },
            timeout=30,
        )

        if not response.ok:
            app.logger.error(
                "OAuth refresh failed: HTTP %s",
                response.status_code,
            )
            return None

        data = response.json()

        entry["access_token"] = data["access_token"]

        entry["expires"] = (
            time.time() + data["expires_in"]
        )

        if data.get("refresh_token"):
            entry["refresh_token"] = data["refresh_token"]

        return entry["access_token"]

    except (requests.RequestException, ValueError):
        app.logger.exception("OAuth refresh error")
        return None


# ============================================================
# HOME PAGE
# ============================================================

HOME_HTML = """
<!doctype html>
<html lang="en">

<head>
<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>Crece Merchant Co — eBay Photos</title>

<style>
body {
    font-family: -apple-system, BlinkMacSystemFont,
                 "Segoe UI", sans-serif;
    max-width: 700px;
    margin: 30px auto;
    padding: 0 18px;
    color: #222;
    line-height: 1.5;
}

h1 {
    font-size: 26px;
}

.card {
    border: 1px solid #ddd;
    border-radius: 12px;
    padding: 20px;
    margin: 20px 0;
}

button, .button {
    display: inline-block;
    background: #1559b7;
    color: white;
    padding: 13px 18px;
    border: none;
    border-radius: 8px;
    font-size: 16px;
    text-decoration: none;
    cursor: pointer;
}

input[type=file] {
    display: block;
    margin: 18px 0;
    max-width: 100%;
}

.note {
    color: #555;
    font-size: 14px;
}

.success {
    color: #18743a;
    font-weight: bold;
}

</style>
</head>

<body>

<h1>Crece Merchant Co</h1>

<h2>eBay Photo Uploader</h2>

<p>
Upload listing photographs directly to eBay
Picture Services.
</p>

{% if not ready %}

<div class="card">

<h3>Configuration required</h3>

<p>
Check your eBay credentials and Flask secret
in Render's environment settings.
</p>

</div>

{% elif not connected %}

<div class="card">

<p>
Connect your eBay seller account to begin.
</p>

<a class="button" href="{{ url_for('login') }}">
Connect to eBay
</a>

</div>

{% else %}

<div class="card">

<p class="success">
Connected to eBay
</p>

<form
    action="{{ url_for('upload') }}"
    method="post"
    enctype="multipart/form-data"
>

<label for="photos">
<strong>Select your listing photographs</strong>
</label>

<input
    id="photos"
    name="photos"
    type="file"
    accept="image/jpeg,image/png,image/webp"
    multiple
    required
>

<p class="note">
Select your main photograph first.
You can upload up to 24 photographs.
</p>

<button type="submit">
Upload photographs
</button>

</form>

</div>

<div class="card">

<form
    method="post"
    action="{{ url_for('disconnect') }}"
>

<button type="submit">
Disconnect eBay
</button>

</form>

</div>

{% endif %}

<p class="note">
This uploader does not publish or modify
your eBay listings.
</p>

<p>
<a href="{{ url_for('privacy') }}">
Privacy policy
</a>
</p>

</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(
        HOME_HTML,
        ready=config_ready(),
        connected=bool(get_access_token()),
    )


# ============================================================
# PRIVACY POLICY
# ============================================================

@app.get("/privacy")
def privacy():
    return """
    <!doctype html>
    <html lang="en">
    <meta name="viewport"
          content="width=device-width,initial-scale=1">

    <title>Privacy Policy — Crece Merchant Co</title>

    <body style="
        font-family:system-ui;
        max-width:700px;
        margin:30px auto;
        padding:0 18px;
        line-height:1.5;
    ">

    <h1>Privacy Policy</h1>

    <p>
    Crece Merchant Co operates a private
    application to upload listing photographs
    to eBay Picture Services.
    </p>

    <p>
    The application uses eBay OAuth to obtain
    permission from its seller account.
    Access and refresh tokens are held
    temporarily in server memory.
    </p>

    <p>
    Uploaded photographs are transmitted
    to eBay. The application does not
    permanently store the photographs.
    </p>

    <p>
    The hosting provider may retain
    ordinary server request logs.
    </p>

    <p>
    Contact:
    crecemerchantco@gmail.com
    </p>

    <p><a href="/">Return to uploader</a></p>

    </body>
    </html>
    """


# ============================================================
# EBAY LOGIN
# ============================================================

@app.get("/login")
def login():
    if not config_ready():
        abort(503, "Application configuration incomplete")

    state = secrets.token_urlsafe(32)

    session["oauth_state"] = state

    params = {
        "client_id": os.environ["EBAY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": os.environ["EBAY_RUNAME"],
        "scope": EBAY_SCOPE,
        "state": state,
    }

    authorization_url = (
        "https://auth.ebay.com/oauth2/authorize?"
        + urlencode(params)
    )

    return redirect(authorization_url)


@app.get("/oauth/callback")
def callback():
    expected_state = session.pop(
        "oauth_state",
        None
    )

    received_state = request.args.get("state", "")

    authorization_code = request.args.get("code")

    if (
        not expected_state
        or not secrets.compare_digest(
            expected_state,
            received_state,
        )
        or not authorization_code
    ):
        abort(400, "Invalid eBay authorization response")

    try:
        response = requests.post(
            EBAY_API + "/identity/v1/oauth2/token",
            auth=ebay_credentials(),
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": os.environ["EBAY_RUNAME"],
            },
            timeout=30,
        )

        if not response.ok:
            app.logger.error(
                "OAuth authorization failed: HTTP %s",
                response.status_code,
            )

            abort(502, "eBay authorization failed")

        data = response.json()

    except requests.RequestException:
        app.logger.exception(
            "OAuth authorization request error"
        )

        abort(502, "Could not contact eBay")

    sid = secrets.token_urlsafe(32)

    session["sid"] = sid

    TOKENS[sid] = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires": (
            time.time() + data["expires_in"]
        ),
    }

    return redirect(url_for("index"))


@app.get("/oauth/declined")
def declined():
    return (
        '<p>eBay authorization was declined.</p>'
        '<p><a href="/">Return to uploader</a></p>'
    )


@app.post("/disconnect")
def disconnect():
    sid = session.pop("sid", None)

    if sid:
        TOKENS.pop(sid, None)

    return redirect(url_for("index"))


# ============================================================
# EBAY MEDIA API
# ============================================================

def upload_one_image(access_token, photo):
    """
    Upload one photograph to eBay Media API.

    Returns:
        (image_url, error_message)
    """

    headers = {
        "Authorization": "Bearer " + access_token,
        "Accept": "application/json",
    }

    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):

        try:
            photo.stream.seek(0)

            response = requests.post(
                UPLOAD_ENDPOINT,
                headers=headers,
                files={
                    "image": (
                        photo.filename,
                        photo.stream,
                        photo.mimetype,
                    )
                },
                timeout=(15, 90),
            )

        except requests.RequestException as exc:
            last_error = (
                "Network request failed: "
                + type(exc).__name__
            )

            app.logger.warning(
                "Image upload attempt %s: %s",
                attempt,
                last_error,
            )

            if attempt < MAX_ATTEMPTS:
                time.sleep(2)
                continue

            return None, last_error

        # Retry temporary eBay server errors.
        if response.status_code in RETRY_STATUS_CODES:

            last_error = (
                f"eBay HTTP {response.status_code}: "
                + response.text[:500]
            )

            app.logger.warning(
                "Temporary eBay upload failure "
                "on attempt %s: HTTP %s",
                attempt,
                response.status_code,
            )

            if attempt < MAX_ATTEMPTS:
                time.sleep(3)
                continue

            return None, last_error

        # Do not retry authorization or malformed requests.
        if not response.ok:

            last_error = (
                f"eBay HTTP {response.status_code}: "
                + response.text[:1000]
            )

            app.logger.error(
                "eBay upload rejected: HTTP %s",
                response.status_code,
            )

            return None, last_error

        # eBay normally returns HTTP 201 and a JSON body.
        try:
            data = response.json() if response.content else {}

        except ValueError:
            data = {}

        image_url = data.get("imageUrl")

        if image_url:
            return image_url, None

        # A successful response may include an image
        # resource URI in the Location header.
        location = response.headers.get("Location", "")

        if not location:
            return (
                None,
                "eBay accepted the image but returned "
                "neither an image URL nor a resource location."
            )

        # Only request eBay's documented API hosts.
        parsed = urlparse(location)

        if location.startswith("/"):
            resource_url = EBAY_API + location

        elif (
            parsed.scheme == "https"
            and parsed.hostname in (
                "api.ebay.com",
                "apim.ebay.com",
            )
        ):
            resource_url = location

        else:
            return (
                None,
                "eBay returned an unexpected image "
                "resource location."
            )

        try:
            details = requests.get(
                resource_url,
                headers=headers,
                timeout=30,
            )

            if not details.ok:
                return (
                    None,
                    "Image uploaded, but retrieving its "
                    f"URL failed: HTTP {details.status_code}"
                )

            image_data = details.json()

            image_url = image_data.get("imageUrl")

            if image_url:
                return image_url, None

            return (
                None,
                "Image uploaded, but eBay did not "
                "return an image URL."
            )

        except (requests.RequestException, ValueError):
            app.logger.exception(
                "Could not retrieve uploaded image details"
            )

            return (
                None,
                "Image uploaded, but the request to "
                "retrieve its URL failed."
            )

    return None, last_error or "Unknown upload error"


# ============================================================
# UPLOAD PHOTOGRAPHS
# ============================================================

@app.post("/upload")
def upload():
    access_token = get_access_token()

    if not access_token:
        return redirect(url_for("login"))

    photos = [
        photo
        for photo in request.files.getlist("photos")
        if photo.filename
    ]

    if not 1 <= len(photos) <= 24:
        abort(
            400,
            "Select between 1 and 24 photographs."
        )

    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/webp",
    }

    results = []

    for index, photo in enumerate(photos, start=1):

        if photo.mimetype not in allowed_types:

            results.append({
                "index": index,
                "filename": photo.filename,
                "url": None,
                "error": "Unsupported image format.",
            })

            continue

        app.logger.info(
            "Starting image %s of %s",
            index,
            len(photos),
        )

        image_url, error = upload_one_image(
            access_token,
            photo,
        )

        results.append({
            "index": index,
            "filename": photo.filename,
            "url": image_url,
            "error": error,
        })

        if image_url:
            app.logger.info(
                "Image %s uploaded successfully",
                index,
            )

        else:
            app.logger.error(
                "Image %s failed: %s",
                index,
                (error or "")[:300],
            )

    return render_template_string(
        RESULTS_HTML,
        results=results,
    )


# ============================================================
# RESULTS PAGE
# ============================================================

RESULTS_HTML = """
<!doctype html>
<html lang="en">

<head>
<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>eBay Photo Upload Results</title>

<style>
body {
    font-family: -apple-system, BlinkMacSystemFont,
                 "Segoe UI", sans-serif;
    max-width: 700px;
    margin: 30px auto;
    padding: 0 18px;
    color: #222;
    line-height: 1.5;
}

h1 {
    font-size: 25px;
}

.photo {
    padding: 18px;
    margin: 18px 0;
    border: 1px solid #ddd;
    border-radius: 10px;
}

.success {
    color: #18743a;
}

.error {
    background: #fff1f1;
    border: 1px solid #efc0c0;
    padding: 12px;
    border-radius: 8px;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
}

.url {
    display: block;
    overflow-wrap: anywhere;
    padding: 10px;
    background: #f4f4f4;
    margin: 12px 0;
}

.button {
    display: inline-block;
    background: #1559b7;
    color: white;
    padding: 13px 18px;
    border-radius: 8px;
    text-decoration: none;
    margin: 14px 0;
}

</style>
</head>

<body>

<h1>Photo Upload Results</h1>

{% set successful = results
    | selectattr("url")
    | list
%}

<p>
<strong>{{ successful | length }}</strong>
of
<strong>{{ results | length }}</strong>
photographs uploaded successfully.
</p>

{% if successful %}

<p>
Your first successful photograph is the
first image URL in the list below.
</p>

{% endif %}

{% for item in results %}

<div class="photo">

<h3>
{{ item.index }}.
{{ item.filename }}
</h3>

{% if item.url %}

<p class="success">
Successfully uploaded
</p>

<a href="{{ item.url }}" target="_blank"
   rel="noopener noreferrer">
View photograph
</a>

<code class="url">
{{ item.url }}
</code>

{% else %}

<p>
<strong>Upload failed</strong>
</p>

<div class="error">
{{ item.error }}
</div>

{% endif %}

</div>

{% endfor %}

<a class="button" href="{{ url_for('index') }}">
Upload another photo
</a>

<p>
<a href="{{ url_for('index') }}">
Return to uploader
</a>
</p>

</body>
</html>
"""


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "configured": bool(config_ready()),
    }


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
