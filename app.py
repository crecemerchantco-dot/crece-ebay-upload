
import os
import secrets
import time
import html
from urllib.parse import urlencode

import requests
from flask import (
    Flask,
    request,
    session,
    redirect,
    render_template_string,
    Response,
    abort,
)

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "dev-only-change-me"
)

app.config.update(
    SESSION_COOKIE_SECURE=(
        os.environ.get("RENDER", "") == "true"
    ),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=80 * 1024 * 1024,
)

# Temporary, in-memory OAuth token storage.
TOKENS = {}

SCOPES = (
    "https://api.ebay.com/oauth/api_scope/"
    "sell.inventory"
)

API = "https://api.ebay.com"


def base():
    return os.environ.get(
        "PUBLIC_BASE_URL",
        "http://localhost:5000"
    ).rstrip("/")


def config_ready():
    required = (
        "EBAY_CLIENT_ID",
        "EBAY_CLIENT_SECRET",
        "EBAY_RUNAME",
        "FLASK_SECRET_KEY",
    )
    return all(os.environ.get(k) for k in required)


def token():
    sid = session.get("sid")
    entry = TOKENS.get(sid)

    if not entry:
        return None

    if entry["expires"] > time.time() + 90:
        return entry["access_token"]

    if not entry.get("refresh_token"):
        return None

    try:
        r = requests.post(
            API + "/identity/v1/oauth2/token",
            auth=(
                os.environ["EBAY_CLIENT_ID"],
                os.environ["EBAY_CLIENT_SECRET"],
            ),
            data={
                "grant_type": "refresh_token",
                "refresh_token": entry["refresh_token"],
                "scope": SCOPES,
            },
            timeout=25,
        )

        if not r.ok:
            app.logger.error(
                "Token refresh failed: HTTP %s",
                r.status_code
            )
            return None

        data = r.json()

        entry.update(
            access_token=data["access_token"],
            expires=time.time() + data["expires_in"],
        )

        if data.get("refresh_token"):
            entry["refresh_token"] = data["refresh_token"]

        return entry["access_token"]

    except requests.RequestException:
        app.logger.exception("Token refresh request failed")
        return None


PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>Crece Merchant Co — eBay Photos</title>

<style>
body {
    font: 16px system-ui, sans-serif;
    max-width: 680px;
    margin: 32px auto;
    padding: 0 18px;
    color: #222;
}

h1 {
    font-size: 26px;
}

a, button {
    color: #0759c9;
}

button {
    padding: 12px 18px;
    font-size: 17px;
    cursor: pointer;
}

input {
    margin: 10px 0;
    max-width: 100%;
}

section {
    border: 1px solid #ddd;
    padding: 18px;
    border-radius: 12px;
    margin: 18px 0;
}

small {
    color: #555;
}
</style>
</head>

<body>

<h1>Crece Merchant Co — eBay photo uploader</h1>

<p>
Upload your item photos directly to eBay.
No listing will be published.
</p>

{% if not ready %}

<section>
<strong>Configuration required.</strong>
<p>
Check your eBay credentials and environment
variables in Render.
</p>
</section>

{% elif not connected %}

<section>
<a href="/login">
Connect your eBay seller account
</a>
</section>

{% else %}

<section>

<strong>Connected to eBay</strong>

<form method="post"
      action="/upload"
      enctype="multipart/form-data">

<p>
<label>
Choose photos:
<br>
<input type="file"
       name="photos"
       multiple
       accept="image/jpeg,image/png,image/webp"
       required>
</label>
</p>

<p>
<small>
Select your main photograph first.
</small>
</p>

<button type="submit">
Upload photos to eBay
</button>

</form>

<p>
<small>
Photos are processed in memory and are
not stored on this server.
</small>
</p>

<form method="post" action="/disconnect">
<button type="submit">
Disconnect
</button>
</form>

</section>

{% endif %}

<p>
<a href="/privacy">Privacy policy</a>
</p>

</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(
        PAGE,
        ready=config_ready(),
        connected=(
            session.get("sid") in TOKENS
        ),
    )


@app.get("/privacy")
def privacy():
    return """
    <h1>Crece Merchant Co eBay Uploader
    — Privacy Policy</h1>

    <p>
    This private, single-seller application is
    used by its owner to upload product images
    to eBay.
    </p>

    <p>
    OAuth credentials and tokens are processed
    on the server. User access and refresh
    tokens are held temporarily in server
    memory only and are lost on restart
    or disconnect.
    </p>

    <p>
    Product photos are sent to eBay and are
    not stored on this server.
    </p>

    <p>
    The application does not collect buyer,
    order, or customer profile data.
    </p>

    <p>
    Uploaded photographs and resulting URLs
    are subject to eBay's policies.
    The hosting provider may retain ordinary
    request logs.
    </p>

    <p>
    Contact: crecemerchantco@gmail.com
    </p>

    <p><a href="/">Back</a></p>
    """


@app.get("/login")
def login():
    if not config_ready():
        abort(503, "Configuration incomplete")

    state = secrets.token_urlsafe(32)

    session["oauth_state"] = state

    params = {
        "client_id": os.environ["EBAY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": os.environ["EBAY_RUNAME"],
        "scope": SCOPES,
        "state": state,
    }

    return redirect(
        "https://auth.ebay.com/oauth2/authorize?"
        + urlencode(params)
    )


@app.get("/oauth/callback")
def callback():
    expected_state = session.pop(
        "oauth_state",
        None
    )

    received_state = request.args.get("state")
    code = request.args.get("code")

    if (
        not expected_state
        or not secrets.compare_digest(
            expected_state,
            received_state or ""
        )
        or not code
    ):
        abort(
            400,
            "Authorization failed or state mismatch"
        )

    try:
        r = requests.post(
            API + "/identity/v1/oauth2/token",
            auth=(
                os.environ["EBAY_CLIENT_ID"],
                os.environ["EBAY_CLIENT_SECRET"],
            ),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": os.environ["EBAY_RUNAME"],
            },
            timeout=25,
        )

    except requests.RequestException:
        app.logger.exception(
            "eBay authorization request failed"
        )
        abort(502, "Authorization request failed")

    if not r.ok:
        app.logger.error(
            "eBay authorization failed: HTTP %s",
            r.status_code
        )
        abort(
            502,
            "eBay authorization failed"
        )

    data = r.json()

    sid = secrets.token_urlsafe(32)

    session["sid"] = sid

    TOKENS[sid] = {
        "access_token": data["access_token"],
        "refresh_token": data.get(
            "refresh_token"
        ),
        "expires": (
            time.time() + data["expires_in"]
        ),
    }

    return redirect("/")


@app.get("/oauth/declined")
def declined():
    return (
        'Authorization declined. '
        '<a href="/">Return</a>'
    )


@app.post("/disconnect")
def disconnect():
    sid = session.pop("sid", None)

    TOKENS.pop(sid, None)

    return redirect("/")


@app.post("/upload")
def upload():
    access = token()

    if not access:
        return redirect("/login")

    photos = [
        f for f in request.files.getlist("photos")
        if f.filename
    ]

    if not photos or len(photos) > 24:
        abort(
            400,
            "Choose between 1 and 24 photos"
        )

    allowed = (
        "image/jpeg",
        "image/png",
        "image/webp",
    )

    for f in photos:
        if f.mimetype not in allowed:
            abort(
                400,
                "JPEG, PNG or WebP only"
            )

    results = []

    for idx, f in enumerate(photos, 1):

        app.logger.info(
            "Uploading photo %s: %s",
            idx,
            f.filename
        )

        try:
            f.stream.seek(0)

            r = requests.post(
                API
                + "/commerce/media/v1_beta/"
                "image/create_image_from_file",
                headers={
                    "Authorization": (
                        "Bearer " + access
                    ),
                    "Accept": "application/json",
                },
                files={
                    "image": (
                        f.filename,
                        f.stream,
                        f.mimetype,
                    )
                },
                timeout=90,
            )

        except requests.RequestException as exc:
            app.logger.error(
                "Photo %s request failed: %s",
                idx,
                type(exc).__name__
            )

            results.append((
                idx,
                f.filename,
                "NETWORK ERROR",
                "",
            ))

            continue

        # Diagnostic logging for unsuccessful uploads.
        if not r.ok:

            error = r.text[:1000]

            app.logger.error(
                "eBay upload failed: HTTP %s — %s",
                r.status_code,
                error
            )

            results.append((
                idx,
                f.filename,
                (
                    "UPLOAD FAILED "
                    f"(HTTP {r.status_code})"
                ),
                error,
            ))

            continue

        # Successful response.
        try:
            data = r.json() if r.content else {}

        except ValueError:
            data = {}

        url = data.get("imageUrl")

        # Some eBay responses provide a resource
        # location rather than an immediate URL.
        if not url:

            loc = r.headers.get(
                "Location",
                ""
            )

            if loc:

                if loc.startswith(
                    "https://api.ebay.com/"
                ):
                    resource_url = loc

                elif loc.startswith("/"):
                    resource_url = API + loc

                else:
                    resource_url = None

                if resource_url:

                    try:
                        gr = requests.get(
                            resource_url,
                            headers={
                                "Authorization": (
                                    "Bearer " + access
                                )
                            },
                            timeout=25,
                        )

                        if gr.ok:
                            url = gr.json().get(
                                "imageUrl"
                            )

                        else:
                            app.logger.error(
                                "Image lookup failed: "
                                "HTTP %s — %s",
                                gr.status_code,
                                gr.text[:1000],
                            )

                    except (
                        requests.RequestException,
                        ValueError,
                    ):
                        app.logger.exception(
                            "Image lookup error"
                        )

        if url:

            app.logger.info(
                "Photo %s uploaded successfully",
                idx
            )

            results.append((
                idx,
                f.filename,
                url,
                "",
            ))

        else:

            app.logger.warning(
                "Photo %s accepted but no URL returned",
                idx
            )

            results.append((
                idx,
                f.filename,
                "Uploaded, but no image URL returned",
                "",
            ))

    # Build the results page.
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        '<meta name="viewport" '
        'content="width=device-width,initial-scale=1">',
        "<title>eBay upload results</title>",
        "<style>",
        "body{font:16px system-ui;"
        "max-width:680px;margin:30px auto;"
        "padding:0 18px}",
        "li{margin:20px 0}",
        "code{overflow-wrap:anywhere}",
        ".error{color:#a00;"
        "background:#fff1f1;padding:12px;"
        "border-radius:8px}",
        "</style>",
        "<h1>eBay image upload results</h1>",
        "<p>The first successfully uploaded image "
        "should be your main photo.</p>",
        "<ol>",
    ]

    for idx, name, result, error in results:

        lines.append("<li>")

        lines.append(
            "<strong>"
            + html.escape(name)
            + "</strong><br>"
        )

        lines.append(
            "<code>"
            + html.escape(result)
            + "</code>"
        )

        if error:

            lines.append(
                '<div class="error">'
                "<strong>eBay error:</strong><br>"
                + html.escape(error)
                + "</div>"
            )

        lines.append("</li>")

    lines.extend([
        "</ol>",
        '<p><a href="/">'
        "Upload another item"
        "</a></p>",
        "</html>",
    ])

    return Response(
        "".join(lines),
        mimetype="text/html",
    )


if __name__ == "__main__":
    app.run(debug=False)
