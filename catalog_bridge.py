"""One explicitly shared spreadsheet; credentials never enter a listing package."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import google.auth.transport.requests
from google.oauth2 import service_account

CATALOG_ID = '17NcKyscAKXdIbUDoWJsMLFNewUehYzjcrnJSCfliUW8'
API = 'https://sheets.googleapis.com/v4/spreadsheets/' + CATALOG_ID
HEADERS = {'A': 'Inventory ID', 'C': 'Item Name', 'Z': 'Listing Title',
           'AA': 'Listing Status', 'AB': 'Asking Price', 'AD': 'Date Listed',
           'AJ': 'Next Action', 'AL': 'Primary Photo URL',
           'AM': 'Additional Photo URLs', 'AN': 'Manual Workflow Status'}
CHANNEL_HEADERS = ['Inventory ID', 'Channel', 'Status', 'Price', 'Date', 'URL', 'Notes', 'Version', 'Updated']


class CatalogError(Exception):
    pass


def col_index(col):
    n = 0
    for c in col:
        n = n * 26 + ord(c) - 64
    return n - 1


def cell(row, col):
    i = col_index(col)
    return row[i] if len(row) > i else ''


class Catalog:
    def __init__(self, credential_json=None, http=None):
        if http is not None:
            self.http = http
        else:
            try:
                info = json.loads(credential_json)
                creds = service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
                self.http = google.auth.transport.requests.AuthorizedSession(creds)
            except (ValueError, TypeError, KeyError):
                raise CatalogError('The catalog connection is not configured correctly.') from None

    def call(self, method, suffix, **kwargs):
        try:
            response = self.http.request(method, API + suffix, timeout=20, **kwargs)
            if response.status_code != 200:
                raise CatalogError('Catalog access failed. Check the app’s access to the master spreadsheet.')
            return response.json()
        except CatalogError:
            raise
        except Exception:
            # Google auth errors can contain credential detail; never return them.
            raise CatalogError('The catalog connection did not complete.') from None

    def read(self, sku):
        data = self.call('GET', '/values:batchGet', params=[('ranges', 'Inventory!A2:AQ'), ('ranges', "'CRECE Channel Listings'!A1:I")])
        values = data.get('valueRanges', [])
        if len(values) != 2:
            raise CatalogError('The catalog layout could not be checked.')
        rows = values[0].get('values', [])
        channels = values[1].get('values', [])
        if not rows or any(cell(rows[0], c) != h for c, h in HEADERS.items()) or not channels or channels[0] != CHANNEL_HEADERS:
            raise CatalogError('Catalog headers changed. Update the integration before publishing.')
        matches = [(n + 2, r) for n, r in enumerate(rows[1:], start=1) if cell(r, 'A') == sku]
        if len(matches) != 1:
            raise CatalogError('The catalog number must identify exactly one existing inventory record.')
        number, row = matches[0]
        ledgers = [(n + 1, r) for n, r in enumerate(channels[1:], start=1) if len(r) > 1 and r[0] == sku and r[1].lower() == 'ebay']
        if len(ledgers) > 1:
            raise CatalogError('Duplicate eBay channel records need reconciliation first.')
        return number, row, ledgers

    def ensure_unlisted(self, sku):
        _, row, ledger = self.read(sku)
        states = [cell(row, c).lower() for c in ('B', 'AA', 'AN')]
        if any(x in ('sold', 'closed', 'listed', 'scheduled', 'remove from inventory') or 'duplicate' in x or 'removed' in x for x in states):
            raise CatalogError('This catalog record is already listed, scheduled, sold, or closed. Reconcile it before creating a listing.')
        if ledger and len(ledger[0][1]) > 2 and ledger[0][1][2].lower() in ('listed', 'active', 'scheduled', 'sold'):
            raise CatalogError('The channel catalog already records an eBay listing for this item.')

    def write_receipt(self, receipt):
        sku = receipt['sku']
        number, row, ledger = self.read(sku)
        observed = receipt['observed']
        status = observed['listingTerms']['lifecycleTerms']['listingStatus']
        if status not in ('ACTIVE', 'SCHEDULED'):
            raise CatalogError('Only verified active or scheduled results can update listing status.')
        if cell(row, 'B').lower() in ('sold', 'closed') or cell(row, 'AN').lower() in ('sold', 'closed'):
            raise CatalogError('The inventory is sold or closed. The receipt was saved; reconcile status manually.')
        label = 'Listed' if status == 'ACTIVE' else 'Scheduled'
        item = observed['items'][0]
        price = item['itemTerms']['pricingTerms']['fixedPrice']['original']['value']
        urls = [x['ebayImageUrl'] for x in observed['sellerProduct']['images']]
        link = 'https://www.ebay.com/itm/' + receipt['listing_id']
        stamp = receipt['verified_at']
        note = 'eBay verified ' + label + '; listing ' + receipt['listing_id'] + '; start ' + str(observed['listingTerms']['lifecycleTerms'].get('startAt', '')) + '; ' + link
        previous = str(cell(row, 'AJ'))
        updates = {'Z': observed['sellerProduct']['title'], 'AA': label, 'AN': label, 'AB': price,
                   'AJ': previous if note in previous else (previous + '\n' + note).strip(),
                   'AL': urls[0], 'AM': '|'.join(urls[1:])}
        if status == 'ACTIVE' and not cell(row, 'AD'):
            updates['AD'] = datetime.fromisoformat(stamp).astimezone(ZoneInfo('America/Detroit')).date().isoformat()
        data = [{'range': f'Inventory!{c}{number}', 'values': [[v]]} for c, v in updates.items()]
        channel_row = [sku, 'eBay', label, price, observed['listingTerms']['lifecycleTerms'].get('startAt', ''), link, 'Verified receipt saved by listing app.', receipt['intent_hash'], stamp]
        if ledger:
            existing = ledger[0][1]
            if len(existing) > 5 and existing[5] and existing[5] != link:
                raise CatalogError('A different eBay listing is recorded in the channel catalog. Reconcile it first.')
            data.append({'range': f"'CRECE Channel Listings'!A{ledger[0][0]}:I{ledger[0][0]}", 'values': [channel_row]})
        self.call('POST', '/values:batchUpdate', json={'valueInputOption': 'RAW', 'data': data})
        if not ledger:
            self.call('POST', "/values/'CRECE%20Channel%20Listings'!A:I:append", params={'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'}, json={'values': [channel_row]})
        # Confirm linkage, rather than treating an HTTP 200 as enough.
        _, written, channels = self.read(sku)
        if cell(written, 'AA') != label or cell(written, 'AN') != label or len(channels) != 1 or channels[0][1][5] != link:
            raise CatalogError('eBay receipt is saved, but catalog confirmation is pending.')
