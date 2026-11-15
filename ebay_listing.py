"""eBay Listing API validation only. No execution or publishing entry point."""
import copy
import io
import json
import re
import secrets
import zipfile

import requests

LISTING_SCOPE = 'https://api.ebay.com/oauth/api_scope/sell.listing'
GRAPHQL_URL = 'https://graphqlapi.ebay.com/graphql'
MAX_PACKAGE_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
VALIDATE_QUERY = '''mutation ValidateListing($input: CreateListingInput!) {
  createListing(input: $input) {
    __typename
    ... on CreateListingValidationSuccess {
      warnings { warningCode warningMessage }
    }
    ... on CreateListingValidationError {
      errorCode errorMessage
      violations { violationCode violationDescription inputPath }
      warnings { warningCode warningMessage }
    }
  }
}'''


def load_package(raw):
    if not raw or len(raw) > MAX_PACKAGE_BYTES:
        raise ValueError('Choose a listing package smaller than 256 KB.')
    try:
        package = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('This file is not a valid listing package.') from None
    if not isinstance(package, dict) or package.get('format') != 'crece-ebay-listing-v1':
        raise ValueError('Choose a CRECE listing package prepared for this app.')
    item = package.get('listing')
    if not isinstance(item, dict):
        raise ValueError('The listing package is missing its listing details.')
    if item.get('marketplace') != 'EBAY_US':
        raise ValueError('This version supports the US marketplace only.')
    sku = item.get('sku')
    if not isinstance(sku, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', sku):
        raise ValueError('The package needs a valid catalog SKU.')
    # The already-listed scale is not our test item.
    if sku == 'MW-2026-200':
        raise ValueError('This scale is already scheduled. Choose a new, unlisted item.')
    product = item.get('product')
    terms = item.get('listingTerms')
    items = item.get('listingItems')
    if not isinstance(product, dict) or not isinstance(terms, dict) or not isinstance(items, dict):
        raise ValueError('The package is missing product, selling terms, or quantity details.')
    title = product.get('title')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
        raise ValueError('The listing title must contain 1–80 characters.')
    if terms.get('listingFormat') != 'FIXED_PRICE':
        raise ValueError('This version supports fixed-price listings only.')
    if not isinstance(items.get('items'), list) or len(items['items']) != 1:
        raise ValueError('This version supports one item per listing.')
    # Never accept a caller-supplied query, endpoint, execution mode or retry key.
    prepared = copy.deepcopy(item)
    prepared['options'] = {'operationMode': 'VALIDATE'}
    prepared['idempotencyKey'] = secrets.token_hex(16)
    return prepared


def load_upload(raw):
    """Read a manifest and ordered real photographs; never extract an archive."""
    if not raw or len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError('Choose a listing package smaller than 64 MB.')
    if not raw.startswith(b'PK'):
        return load_package(raw), []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [e.filename for e in entries]
            if len(names) != len(set(names)) or len(names) > 25:
                raise ValueError('The package has duplicate files or too many photographs.')
            if sum(e.file_size for e in entries) > MAX_BUNDLE_BYTES:
                raise ValueError('The expanded package is too large.')
            if 'listing.json' not in names or archive.getinfo('listing.json').file_size > MAX_PACKAGE_BYTES:
                raise ValueError('The package is missing its listing details.')
            manifest = archive.read('listing.json')
            prepared = load_package(manifest)
            paths = json.loads(manifest).get('photos')
            if not isinstance(paths, list) or not 1 <= len(paths) <= 24 or len(set(paths)) != len(paths):
                raise ValueError('The package needs 1–24 ordered photographs.')
            if set(names) != {'listing.json', *paths}:
                raise ValueError('The package contains unexpected files.')
            photos = []
            for path in paths:
                if not isinstance(path, str) or not re.fullmatch(r'photos/[A-Za-z0-9_-]+\.(jpg|jpeg|png)', path):
                    raise ValueError('The package contains an invalid photograph path.')
                if not 0 < archive.getinfo(path).file_size <= 10 * 1024 * 1024:
                    raise ValueError('Each photograph must be smaller than 10 MB.')
                photos.append((path.rsplit('/', 1)[-1], archive.read(path)))
            return prepared, photos
    except (zipfile.BadZipFile, KeyError, RuntimeError, TypeError, NotImplementedError):
        raise ValueError('The listing package could not be read. Ask for a replacement file.') from None


def validate_listing(seller_token, prepared, http=None):
    """Force validation even if a caller tries to supply EXECUTE."""
    item = copy.deepcopy(prepared)
    item['options'] = {'operationMode': 'VALIDATE'}
    http = http or requests.Session()
    result = {'ok': False, 'published': False, 'state': 'unverified',
              'errors': [], 'warnings': []}
    try:
        response = http.post(GRAPHQL_URL,
            headers={'Authorization': 'Bearer ' + seller_token,
                     'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US', 'Content-Type': 'application/json'},
            json={'query': VALIDATE_QUERY, 'variables': {'input': item}},
            timeout=(5, 35), allow_redirects=False)
        if response.status_code != 200:
            result['errors'] = ['eBay could not validate the listing (HTTP %s). Check app permissions if access was denied.' % response.status_code]
            return result
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        # A GraphQL HTTP 200 is not evidence of successful validation.
        if payload.get('errors'):
            result['errors'] = ['eBay rejected the API request. The app may need different permissions or a schema update.']
            return result
        data = payload.get('data')
        operation = data.get('createListing') if isinstance(data, dict) else None
        if not isinstance(operation, dict):
            raise ValueError()
        kind = operation.get('__typename')
        for warning in operation.get('warnings') or []:
            result['warnings'].append(str(warning.get('warningCode', '')) + ': ' + str(warning.get('warningMessage', '')))
        if kind == 'CreateListingValidationSuccess':
            result.update(ok=True, state='ebay_validation_passed')
        elif kind == 'CreateListingValidationError':
            result['state'] = 'ebay_validation_failed'
            result['errors'].append(str(operation.get('errorCode', '')) + ': ' + str(operation.get('errorMessage', '')))
            for violation in operation.get('violations') or []:
                path = '.'.join(str(p) for p in violation.get('inputPath') or [])
                result['errors'].append(str(violation.get('violationCode', '')) + ': ' + str(violation.get('violationDescription', '')) + (' [' + path + ']' if path else ''))
        else:
            result['errors'] = ['eBay returned an unexpected result. Stop and investigate before any further submission.']
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        result['ok'] = False
        result['state'] = 'unverified'
        result['errors'] = ['The validation check did not complete. No publishing request was sent.']
    return result
