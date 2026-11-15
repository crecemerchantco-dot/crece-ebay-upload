"""Read-only eBay discovery and local preflight. No listing-write methods."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit

import requests

API = 'https://api.ebay.com'
ACCOUNT_SCOPE = 'https://api.ebay.com/oauth/api_scope/sell.account.readonly'
BASE_SCOPE = 'https://api.ebay.com/oauth/api_scope'


class CheckError(Exception):
    """Only sanitized status information may cross the UI boundary."""

    def __init__(self, status=None):
        self.status = status
        if status in (401, 403):
            message = 'eBay authorization is missing or insufficient. Reconnect with account checks enabled.'
        elif status == 429:
            message = 'eBay rate limit reached. Try again later.'
        else:
            message = 'The eBay check could not complete. No listing was created.'
        super().__init__(message)


class EbayChecks:
    def __init__(self, seller_token, app_token=None, http=None):
        self.seller_token = seller_token
        self.app_token = app_token
        self.http = http or requests.Session()

    def _get(self, path, params=None, application=False):
        token = self.app_token if application else self.seller_token
        if not token:
            raise CheckError(401)
        try:
            response = self.http.get(
                API + path, params=params,
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
                timeout=(5, 20), allow_redirects=False)
            if response.status_code != 200:
                raise CheckError(response.status_code)
            payload = response.json()
            if not isinstance(payload, dict):
                raise CheckError()
            return payload
        except (requests.RequestException, ValueError) as exc:
            raise CheckError() from exc

    def policies(self):
        result = {}
        # Separate failures so an absent permission doesn't conceal other results.
        for kind in ('fulfillment', 'return', 'payment'):
            try:
                data = self._get('/sell/account/v1/' + kind + '_policy',
                                 {'marketplace_id': 'EBAY_US'})
                result[kind] = {'ok': True, 'policies': [
                    {'id': str(p.get(kind + 'PolicyId', '')), 'name': p.get('name', '')}
                    for p in data.get(kind + 'Policies', [])]}
            except CheckError as exc:
                result[kind] = {'ok': False, 'error': str(exc)}
        return result

    def category(self, category_id):
        if not isinstance(category_id, str) or not re.fullmatch(r'[1-9][0-9]{0,11}', category_id):
            raise ValueError('Enter a numeric eBay category ID.')
        tree = self._get('/commerce/taxonomy/v1/get_default_category_tree_id',
                         {'marketplace_id': 'EBAY_US'}, application=True)
        tree_id = str(tree.get('categoryTreeId', ''))
        if not re.fullmatch(r'[0-9]+', tree_id):
            raise CheckError()
        data = self._get('/commerce/taxonomy/v1/category_tree/' + tree_id +
                         '/get_item_aspects_for_category', {'category_id': category_id},
                         application=True)
        return {'categoryId': category_id, 'categoryTreeId': tree_id,
                'aspects': data.get('aspects', [])}


def preflight(item, category, now=None):
    """Validate an intake against fetched aspects; this is NOT eBay acceptance."""
    if not isinstance(item, dict):
        raise ValueError('Provide a JSON object for one item.')
    errors = []
    warnings = []
    if not isinstance(item.get('sku'), str) or not item['sku'].strip():
        errors.append('Inventory SKU is required.')
    title = item.get('title')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
        errors.append('Title must contain 1–80 characters.')
    if not isinstance(item.get('description'), str) or not item['description'].strip():
        errors.append('Description is required.')
    try:
        price = Decimal(str(item.get('price')))
        if not price.is_finite() or price <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        errors.append('Price must be a positive finite number.')
    quantity = item.get('quantity')
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        errors.append('Quantity must be a positive integer.')
    if str(item.get('categoryId')) != category['categoryId']:
        errors.append('Category does not match the fetched requirements.')
    urls = item.get('imageUrls')
    if not isinstance(urls, list) or not 1 <= len(urls) <= 24:
        errors.append('Provide 1–24 image URLs.')
    else:
        seen = set()
        for url in urls:
            if not isinstance(url, str):
                errors.append('Every image URL must be a string.')
                continue
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme == 'https' and parsed.hostname == 'i.ebayimg.com' and parsed.path and not parsed.username and not parsed.password and parsed.port in (None, 443) and not any(c.isspace() for c in url)
            except ValueError:
                valid = False
            if not valid:
                errors.append('Use actual HTTPS eBay-hosted image URLs.')
            if url in seen:
                errors.append('Remove duplicate image URLs.')
            seen.add(url)
        warnings.append('Image reachability, image identity and expiration still require verification.')
    aspects = item.get('aspects', {})
    if not isinstance(aspects, dict):
        errors.append('Item specifics must be a JSON object.')
        aspects = {}
    for aspect in category.get('aspects', []):
        name = aspect.get('localizedAspectName', '')
        constraint = aspect.get('aspectConstraint', {})
        supplied = aspects.get(name, [])
        values = [supplied] if isinstance(supplied, str) else supplied
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            errors.append('Invalid values for item specific: ' + name)
            values = []
        if constraint.get('aspectRequired') and not values:
            errors.append('Missing required item specific: ' + name)
        if constraint.get('aspectMode') == 'SELECTION_ONLY':
            allowed = {v.get('localizedValue') for v in aspect.get('aspectValues', [])}
            if any(v not in allowed for v in values):
                errors.append('Choose an allowed value for item specific: ' + name)
    schedule = item.get('scheduleTime')
    if schedule:
        try:
            when = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
            if when.tzinfo is None or when <= (now or datetime.now(timezone.utc)):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            errors.append('Scheduled time must include a timezone and be in the future.')
    warnings.append('Condition eligibility, policies, package values, fees and duplicate listings are not checked by this local preflight. eBay validation is still required.')
    return {'ok': not errors, 'state': 'local_preflight_only', 'errors': errors,
            'warnings': warnings, 'published': False}
