import os, secrets, time, html
from urllib.parse import urlencode
import requests
from flask import Flask, request, session, redirect, url_for, render_template_string, Response, abort

app=Flask(__name__)
app.secret_key=os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-me')
app.config.update(SESSION_COOKIE_SECURE=os.environ.get('RENDER','')=='true', SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=80*1024*1024)
# Deliberately volatile, single-process token store: no database, no persistent eBay user data.
TOKENS={}
SCOPES='https://api.ebay.com/oauth/api_scope/sell.inventory'
API='https://api.ebay.com'

def base():
    return os.environ.get('PUBLIC_BASE_URL','http://localhost:5000').rstrip('/')

def config_ready():
    return all(os.environ.get(k) for k in ('EBAY_CLIENT_ID','EBAY_CLIENT_SECRET','EBAY_RUNAME')) and os.environ.get('FLASK_SECRET_KEY')

def token():
    sid=session.get('sid'); entry=TOKENS.get(sid)
    if not entry:return None
    if entry['expires']>time.time()+90:return entry['access_token']
    if not entry.get('refresh_token'):return None
    r=requests.post(API+'/identity/v1/oauth2/token',auth=(os.environ['EBAY_CLIENT_ID'],os.environ['EBAY_CLIENT_SECRET']),data={'grant_type':'refresh_token','refresh_token':entry['refresh_token'],'scope':SCOPES},timeout=25)
    if not r.ok:return None
    data=r.json();entry.update(access_token=data['access_token'],expires=time.time()+data['expires_in']);return entry['access_token']

PAGE='''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Crece Merchant Co — eBay Photos</title><style>body{font:16px system-ui;max-width:680px;margin:32px auto;padding:0 18px;color:#222}h1{font-size:26px}a,button{color:#0759c9}button{padding:12px 18px;font-size:17px}input{margin:10px 0}section{border:1px solid #ddd;padding:18px;border-radius:12px;margin:18px 0}small{color:#555}li{margin:10px 0}</style><h1>Crece Merchant Co — eBay photo uploader</h1><p>Upload your item photos directly to eBay Picture Services, in the order selected. No listing will be published.</p>{% if not ready %}<section>Configuration required. Follow README.md to add your eBay credentials and RuName to your host.</section>{% elif not connected %}<section><a href="/login">Connect your eBay seller account</a></section>{% else %}<section><strong>Connected to eBay</strong><form method="post" action="/upload" enctype="multipart/form-data"><label>Choose photos (select your front cover first):<br><input type="file" name="photos" multiple accept="image/jpeg,image/png,image/webp" required></label><br><button>Upload photos to eBay</button></form><p><small>Images are processed in memory and are not stored on this server. Uploaded images remain in eBay Picture Services.</small></p><form method="post" action="/disconnect"><button>Disconnect</button></form></section>{% endif %}<p><a href="/privacy">Privacy policy</a></p></html>'''

@app.get('/')
def index():return render_template_string(PAGE,ready=config_ready(),connected=bool(session.get('sid') in TOKENS))

@app.get('/privacy')
def privacy():
    return '''<h1>Crece Merchant Co eBay Uploader — Privacy Policy</h1><p>This private, single-seller application is used by its owner to upload product images to eBay. It requests seller authorization to upload photos. OAuth credentials and tokens are processed on the server; user access and refresh tokens are held temporarily in server memory only and are lost on restart or disconnect. Product photos are streamed to eBay and not stored on this server. The application does not collect buyer, order, or customer profile data, and does not retain an eBay user database. Uploaded photos and their resulting URLs are subject to eBay's policies. The hosting provider may retain ordinary request logs; do not place credentials or tokens in URLs or logs. Contact: crecemerchantco@gmail.com.</p><p><a href="/">Back</a></p>'''

@app.get('/login')
def login():
    if not config_ready():abort(503)
    state=secrets.token_urlsafe(32);session['oauth_state']=state
    params={'client_id':os.environ['EBAY_CLIENT_ID'],'response_type':'code','redirect_uri':os.environ['EBAY_RUNAME'],'scope':SCOPES,'state':state}
    return redirect('https://auth.ebay.com/oauth2/authorize?'+urlencode(params))

@app.get('/oauth/callback')
def callback():
    if not session.pop('oauth_state',None)==request.args.get('state') or not request.args.get('code'):abort(400,'Authorization failed or state mismatch')
    r=requests.post(API+'/identity/v1/oauth2/token',auth=(os.environ['EBAY_CLIENT_ID'],os.environ['EBAY_CLIENT_SECRET']),data={'grant_type':'authorization_code','code':request.args['code'],'redirect_uri':os.environ['EBAY_RUNAME']},timeout=25)
    if not r.ok:abort(502,'eBay authorization failed. Check RuName and app configuration.')
    data=r.json();sid=secrets.token_urlsafe(32);session['sid']=sid
    TOKENS[sid]={'access_token':data['access_token'],'refresh_token':data.get('refresh_token'),'expires':time.time()+data['expires_in']}
    return redirect('/')

@app.get('/oauth/declined')
def declined():return 'Authorization declined. <a href="/">Return</a>'

@app.post('/disconnect')
def disconnect():
    TOKENS.pop(session.pop('sid',None),None);return redirect('/')

@app.post('/upload')
def upload():
    access=token()
    if not access:return redirect('/login')
    photos=request.files.getlist('photos')
    if not photos or len(photos)>24:abort(400,'Choose 1–24 photos')
    results=[]
    for idx,f in enumerate(photos,1):
        if f.mimetype not in ('image/jpeg','image/png','image/webp'):abort(400,'JPEG, PNG or WebP only')
        r=requests.post(API+'/commerce/media/v1_beta/image/create_image_from_file',headers={'Authorization':'Bearer '+access,'Accept':'application/json'},files={'image':(f.filename,f.stream,f.mimetype)},timeout=90)
        if not r.ok:
            results.append((idx,f.filename,'UPLOAD FAILED (HTTP '+str(r.status_code)+')'));continue
        data=r.json() if r.content else {}; url=data.get('imageUrl')
        if not url:
            loc=r.headers.get('Location','')
            if loc:
                gr=requests.get(loc if loc.startswith('https://') else API+loc,headers={'Authorization':'Bearer '+access},timeout=25)
                if gr.ok:url=gr.json().get('imageUrl')
        results.append((idx,f.filename,url or 'Uploaded, but no image URL returned'))
    # Return URLs directly in browser; never persist uploaded files or eBay user data.
    lines=['<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><h1>eBay image upload results</h1><p>Copy these URLs into your listing CSV. The first image is your main photo.</p><ol>']
    for idx,name,url in results:
        lines.append('<li>'+html.escape(name)+'<br><code style="overflow-wrap:anywhere">'+html.escape(url)+'</code></li>')
    lines.append('</ol><a href="/">Upload another item</a>')
    return Response(''.join(lines),mimetype='text/html')

if __name__=='__main__':app.run(debug=False)
