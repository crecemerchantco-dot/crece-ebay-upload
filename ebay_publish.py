"""One explicitly authorized Kitcho scheduling test, not a general publisher.

The committed approval is a durable, immutable creation intent. eBay holds the
future schedule; this app retains no unattended job or durable seller token.
"""
import copy
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from ebay_listing import GRAPHQL_URL, validate_listing

APPROVAL_PATH = Path(__file__).parent / 'approved' / 'kitcho.json'
SCHEDULE_LOCK = threading.Lock()
CREATE_QUERY = '''mutation ScheduleApprovedBook($input: CreateListingInput!) {
 createListing(input: $input) {
  __typename
  ... on CreateListingSuccess {
   listing { listingId sku listingTerms { lifecycleTerms { listingStatus startAt } } }
   warnings { warningCode warningMessage }
  }
  ... on CreateListingValidationError {
   errorCode errorMessage
   violations { violationCode violationDescription inputPath }
  }
 }
}'''
SEARCH_QUERY = '''query FindApprovedBook($active: SellerActiveListingSearchInput!, $scheduled: SellerScheduledListingSearchInput!) {
 sellerListingSearch {
  activeListings(searchInput: $active) {
   __typename
   ... on SellerListings { listings { listingId } pagination { nextCursor totalElements } }
  }
  scheduledListings(searchInput: $scheduled) {
   __typename
   ... on SellerListings { listings { listingId } pagination { nextCursor totalElements } }
  }
 }
}'''
VERIFY_QUERY = '''query VerifyApprovedBook($input: SellerListingsInput!) {
 sellerListings(input: $input) {
  __typename
  ... on ListingsSuccess {
   listings {
    __typename
    ... on ListingSuccess {
     listingId
     listing {
      sellerProduct { title images { ebayImageUrl } }
      listingTerms { lifecycleTerms { listingStatus startAt } }
      items { sku itemTerms { pricingTerms { fixedPrice { original { value currency } } } availability { quantityAvailable } } }
     }
    }
   }
  }
 }
}'''


class PublishError(Exception):
    pass


def approval():
    record = json.loads(APPROVAL_PATH.read_text())
    item = record['listing']
    digest = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != record['payload_sha256'] or item['sku'] != 'MW-2026-296' or item['listingItems']['items'][0]['itemTerms']['pricingTerms']['fixedPrice'] != '29.95':
        raise PublishError('The approved listing details do not match. Publishing is disabled.')
    if item['listingTerms']['lifecycleTerms']['scheduledToStartAt'] != '2026-11-16T12:00:00Z':
        raise PublishError('The approved schedule does not match. Publishing is disabled.')
    return record


def post(token, query, variables, http):
    try:
        response = http.post(GRAPHQL_URL,
            headers={'Authorization': 'Bearer ' + token, 'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US', 'Content-Type': 'application/json'},
            json={'query': query, 'variables': variables}, timeout=(5, 35), allow_redirects=False)
        if response.status_code != 200:
            raise PublishError('eBay request failed (HTTP %s). Do not submit again until the listing status is checked.' % response.status_code)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('errors') or not isinstance(payload.get('data'), dict):
            raise PublishError('eBay could not complete the request. Do not assume a listing was created or absent.')
        return payload['data']
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise PublishError('The eBay response was interrupted. The outcome may be unknown; check status before another submission.') from exc


def find_existing(token, http):
    filters = {'filters': {'skus': ['MW-2026-296'], 'marketplaces': ['EBAY_US']}, 'pagination': {'maxPageSize': 10}}
    data = post(token, SEARCH_QUERY, {'active': filters, 'scheduled': filters}, http).get('sellerListingSearch')
    if not isinstance(data, dict):
        raise PublishError('The duplicate check did not complete. Nothing will be submitted.')
    found = set()
    for field in ('activeListings', 'scheduledListings'):
        branch = data.get(field)
        if not isinstance(branch, dict) or branch.get('__typename') != 'SellerListings':
            raise PublishError('The duplicate check was rejected. Nothing will be submitted.')
        page = branch.get('pagination') or {}
        matches = branch.get('listings')
        if not isinstance(matches, list) or page.get('nextCursor') or page.get('totalElements') != len(matches):
            raise PublishError('The duplicate check is incomplete. Nothing will be submitted.')
        for match in matches:
            if not match.get('listingId'):
                raise PublishError('The duplicate check returned an invalid listing.')
            found.add(match['listingId'])
    return sorted(found)


def verify(token, listing_id, http):
    output = post(token, VERIFY_QUERY, {'input': {'listings': [{'listingId': listing_id}]}}, http).get('sellerListings')
    if not isinstance(output, dict) or output.get('__typename') != 'ListingsSuccess':
        raise PublishError('eBay accepted a listing ID, but its current state could not be verified. Do not create another listing.')
    listings = output.get('listings') or []
    if len(listings) != 1 or listings[0].get('__typename') != 'ListingSuccess' or listings[0].get('listingId') != listing_id:
        raise PublishError('eBay returned an incomplete verification result. Do not create another listing.')
    return listings[0]['listing']


def schedule_approved_book(token, http=None, now=None):
    if not SCHEDULE_LOCK.acquire(blocking=False):
        raise PublishError('A scheduling request is already running. Wait for its result before trying again.')
    try:
        return _schedule_approved_book(token, http, now)
    finally:
        SCHEDULE_LOCK.release()


def _schedule_approved_book(token, http=None, now=None):
    http = http or requests.Session()
    record = approval()
    existing = find_existing(token, http)
    if len(existing) > 1:
        raise PublishError('More than one listing uses this catalog number. Stop and reconcile them.')
    if existing:
        item = verify(token, existing[0], http)
        return {'created': False, 'listing_id': existing[0], 'observed': item, 'message': 'Existing listing found; no new listing was created.'}
    item = copy.deepcopy(record['listing'])
    when = datetime.fromisoformat(item['listingTerms']['lifecycleTerms']['scheduledToStartAt'].replace('Z', '+00:00'))
    if when <= (now or datetime.now(timezone.utc)):
        raise PublishError('The approved publication time has passed. Choose a new date before scheduling.')
    checked = validate_listing(token, item, http)
    if not checked['ok']:
        raise PublishError('eBay validation did not pass. No publishing request was sent. ' + ' '.join(checked['errors']))
    # Fixed approved payload, including its durable key. Never derive a new key
    # on retry and never accept price, schedule or listing data from the request.
    item['options'] = {'operationMode': 'EXECUTE'}
    operation = post(token, CREATE_QUERY, {'input': item}, http).get('createListing')
    if not isinstance(operation, dict) or operation.get('__typename') != 'CreateListingSuccess':
        raise PublishError('eBay did not confirm creation. Check listing status before another submission.')
    listing_id = (operation.get('listing') or {}).get('listingId')
    if not listing_id:
        raise PublishError('eBay did not return a listing ID. Check status before another submission.')
    try:
        observed = verify(token, listing_id, http)
    except PublishError:
        return {'created': True, 'listing_id': listing_id, 'observed': None,
                'message': 'Creation accepted; current status verification is pending. Do not create another listing.'}
    return {'created': True, 'listing_id': listing_id, 'observed': observed, 'message': 'Creation accepted and listing details retrieved.'}
