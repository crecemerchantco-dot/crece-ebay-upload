"""Reusable publisher: disclosed terms, selected day, durable intent, reconciliation."""
import copy
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import requests
from catalog_bridge import CatalogError
from ebay_listing import validate_listing
from ebay_publish import PublishError, post, verify, SEARCH_QUERY, CREATE_QUERY

EASTERN = ZoneInfo('America/Detroit')


def digest(item):
    value = copy.deepcopy(item)
    value.pop('idempotencyKey', None)
    value.pop('options', None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def review(item):
    try:
        entries = item['listingItems']['items']
        if len(entries) != 1 or entries[0]['sku'] != item['sku'] or entries[0]['itemTerms']['availability']['quantityAvailable'] != 1:
            raise ValueError()
        price = Decimal(entries[0]['itemTerms']['pricingTerms']['fixedPrice'])
        if not price.is_finite() or price <= 0 or price != price.quantize(Decimal('.01')):
            raise ValueError()
        if not re.fullmatch(r'MW-\d{4}-\d{3}[A-Z]?', item['sku']):
            raise ValueError()
        urls = item['product']['imageUrls']
        if not isinstance(urls, list) or not 1 <= len(urls) <= 24 or any(not isinstance(u, str) or not u.startswith('https://i.ebayimg.com/') for u in urls):
            raise ValueError()
        terms = item['listingTerms']
        shipping = terms['fulfillmentTerms']['shippingTerms']['businessPolicy']['businessPolicyId']
        returns = terms['returnTerms']['businessPolicyId']
        offer = terms.get('bestOfferTerms', {})
        # Display all supplied offer thresholds verbatim, rather than silently
        # assuming an enabled best offer has no automatic acceptance behavior.
        return {'sku': item['sku'], 'title': item['product']['title'], 'price': str(price),
                'shipping_policy': shipping, 'return_policy': returns, 'offer': offer,
                'photo_count': len(urls)}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise PublishError('The package needs one cataloged item, quantity one, a valid price, eBay photographs and saved shipping/return policies.') from None


def select_schedule(item, day, clock='', now=None, immediate=False):
    result = copy.deepcopy(item)
    lifecycle = result['listingTerms']['lifecycleTerms']
    lifecycle.pop('scheduledToStartAt', None)
    now = now or datetime.now(timezone.utc)
    if not immediate:
        if not day:
            raise PublishError('Choose a publication day. A blank time means 7 a.m. Eastern.')
        try:
            local = datetime.fromisoformat(day + 'T' + (clock or '07:00'))
            if local.tzinfo is not None:
                raise ValueError()
            aware = local.replace(tzinfo=EASTERN)
            # Reject ambiguous/nonexistent DST hours; 7 a.m. is unambiguous.
            if aware.utcoffset() != local.replace(tzinfo=EASTERN, fold=1).utcoffset() or aware.astimezone(timezone.utc).astimezone(EASTERN).replace(tzinfo=None) != local:
                raise ValueError()
            utc = aware.astimezone(timezone.utc)
            if utc <= now:
                raise PublishError('Choose a future publication time.')
            lifecycle['scheduledToStartAt'] = utc.isoformat().replace('+00:00', 'Z')
        except ValueError:
            raise PublishError('Choose a valid date and Eastern time. Daylight-saving transition hours need a different time.') from None
    result['options'] = {'operationMode': 'VALIDATE'}
    return result


def find_existing(token, sku, http):
    filters = {'filters': {'skus': [sku], 'marketplaces': ['EBAY_US']}, 'pagination': {'maxPageSize': 10}}
    data = post(token, SEARCH_QUERY, {'active': filters, 'scheduled': filters}, http).get('sellerListingSearch')
    if not isinstance(data, dict):
        raise PublishError('eBay duplicate checking is unavailable; nothing was submitted.')
    found = set()
    for field in ('activeListings', 'scheduledListings'):
        branch = data.get(field) or {}
        page = branch.get('pagination') or {}
        matches = branch.get('listings')
        if branch.get('__typename') != 'SellerListings' or not isinstance(matches, list) or page.get('nextCursor') or page.get('totalElements') != len(matches):
            raise PublishError('The eBay duplicate check was incomplete. Nothing was submitted.')
        for match in matches:
            if not match.get('listingId'):
                raise PublishError('eBay returned an invalid duplicate check.')
            found.add(match['listingId'])
    if len(found) > 1:
        raise PublishError('Multiple eBay listings use this catalog number. Reconcile them first.')
    return sorted(found)


def check_observed(item, observed):
    try:
        products = observed['sellerProduct']
        terms = observed['listingTerms']['lifecycleTerms']
        expected = item['listingTerms']['lifecycleTerms'].get('scheduledToStartAt')
        entries = observed['items']
        actual = entries[0]
        price = actual['itemTerms']['pricingTerms']['fixedPrice']['original']
        if len(entries) != 1 or actual['sku'] != item['sku'] or actual['itemTerms']['availability']['quantityAvailable'] != 1:
            raise ValueError()
        if Decimal(price['value']) != Decimal(review(item)['price']) or price['currency'] != 'USD' or products['title'] != item['product']['title']:
            raise ValueError()
        if [x['ebayImageUrl'] for x in products['images']] != item['product']['imageUrls']:
            raise ValueError()
        if terms['listingStatus'] not in ('ACTIVE', 'SCHEDULED'):
            raise ValueError()
        if expected and datetime.fromisoformat(terms['startAt'].replace('Z', '+00:00')) != datetime.fromisoformat(expected.replace('Z', '+00:00')):
            raise ValueError()
        if not expected and terms['listingStatus'] != 'ACTIVE':
            raise ValueError()
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise PublishError('A listing exists, but its details differ or verification is incomplete. Its receipt is saved. Do not submit again.') from None


def sync_catalog(record, entry, catalog):
    try:
        catalog.write_receipt(record)
        record['catalog_state'] = 'saved'
        record.pop('catalog_error', None)
    except CatalogError as exc:
        record['catalog_state'] = 'pending'
        record['catalog_error'] = str(exc)
    entry.save(record)
    return record


def run(token, item, journal, catalog, http=None, now=None, reconcile_only=False, policy_review=None):
    """Never automatically retry EXECUTE after SUBMITTING, even if search is empty."""
    review(item)
    http = http or requests.Session()
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with journal.locked(item['sku']) as entry:
        record = entry.get()
        if record:
            # Recovery always uses the saved original, never a re-uploaded payload.
            item = record['listing']
            if record['state'] == 'verified':
                return sync_catalog(record, entry, catalog)
            if record['state'] in ('submitting', 'accepted', 'unknown', 'existing'):
                candidates = [record['listing_id']] if record.get('listing_id') else find_existing(token, item['sku'], http)
                if not candidates:
                    raise PublishError('The earlier submission is unresolved. No new request was sent; reconcile with Seller Hub.')
                return finish(token, item, record, candidates[0], entry, catalog, http, stamp)
            # Any durable pending intent requires explicit recovery, not creation.
            raise PublishError('An earlier intent is saved. Review it before starting another submission.')
        if reconcile_only:
            raise PublishError('There is no saved submission to recover for this catalog number.')
        catalog.ensure_unlisted(item['sku'])
        existing = find_existing(token, item['sku'], http)
        if existing:
            # Do not overwrite inventory with an unlinked pre-existing listing.
            raise PublishError('An eBay listing already uses this catalog number. No new listing was created; reconcile its catalog linkage first.')
        expected = item['listingTerms']['lifecycleTerms'].get('scheduledToStartAt')
        if expected and datetime.fromisoformat(expected.replace('Z', '+00:00')) <= (now or datetime.now(timezone.utc)):
            raise PublishError('The selected time has passed. Choose a new date before submitting.')
        checked = validate_listing(token, item, http)
        if not checked['ok']:
            raise PublishError('eBay validation did not pass. No publishing request was sent. ' + ' '.join(checked['errors']))
        # Recheck catalog just before committing intent; eBay checks do not lock
        # other sales channels. Only this app's per-SKU submissions are serialized.
        catalog.ensure_unlisted(item['sku'])
        item = copy.deepcopy(item)
        item['idempotencyKey'] = secrets.token_hex(16)
        record = {'sku': item['sku'], 'listing': item, 'intent_hash': digest(item),
                  'state': 'submitting', 'created_at': stamp, 'validation': checked,
                  'catalog_state': 'pending', 'approval': review(item), 'policy_review': policy_review}
        entry.save(record)  # Must commit before any EXECUTE request.
        payload = copy.deepcopy(item)
        payload['options'] = {'operationMode': 'EXECUTE'}
        try:
            result = post(token, CREATE_QUERY, {'input': payload}, http).get('createListing') or {}
            listing_id = (result.get('listing') or {}).get('listingId')
            if result.get('__typename') != 'CreateListingSuccess' or not listing_id:
                raise PublishError('Creation was not confirmed. Check the saved receipt before any further action.')
        except PublishError:
            record['state'] = 'unknown'
            entry.save(record)
            raise
        record.update(state='accepted', listing_id=listing_id)
        entry.save(record)
        return finish(token, item, record, listing_id, entry, catalog, http, stamp)


def finish(token, item, record, listing_id, entry, catalog, http, stamp):
    # Save identifier before verification, even when recovering a lost response.
    record.update(listing_id=listing_id, state='accepted')
    entry.save(record)
    observed = verify(token, listing_id, http)
    record['observed'] = observed
    entry.save(record)
    check_observed(item, observed)
    record.update(state='verified', verified_at=stamp)
    entry.save(record)
    return sync_catalog(record, entry, catalog)
