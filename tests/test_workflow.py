import copy
import json
import os
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import requests
from werkzeug.security import generate_password_hash
import app as web
from ebay_publish import approval, PublishError
from catalog_bridge import Catalog, CatalogError, HEADERS, CHANNEL_HEADERS, col_index
from resale_workflow import run, select_schedule, review, digest
from test_publish import EMPTY, response

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


def item():
    value = copy.deepcopy(approval()['listing'])
    value['sku'] = 'MW-2026-999'
    value['listingItems']['items'][0]['sku'] = value['sku']
    return value


def observed(value):
    return {'sellerProduct': {'title': value['product']['title'], 'images': [{'ebayImageUrl': x} for x in value['product']['imageUrls']]},
        'listingTerms': {'lifecycleTerms': {'listingStatus': 'SCHEDULED', 'startAt': value['listingTerms']['lifecycleTerms']['scheduledToStartAt']}},
        'items': [{'sku': value['sku'], 'itemTerms': {'availability': {'quantityAvailable': 1}, 'pricingTerms': {'fixedPrice': {'original': {'value': '29.95', 'currency': 'USD'}}}}}]}


class MemoryJournal:
    """Test double; production only uses PostgreSQL, never this adapter."""
    record = None
    def __init__(self): self.record = None; self.saved = []
    @contextmanager
    def locked(self, sku): yield self
    def get(self): return copy.deepcopy(self.record)
    def save(self, record):
        self.record = copy.deepcopy(record)
        self.saved.append(copy.deepcopy(record))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.journal = MemoryJournal()
        self.catalog = Mock()
        self.http = Mock()
        self.item = item()

    def success(self):
        return [response(EMPTY), response({'data': {'createListing': {'__typename': 'CreateListingValidationSuccess'}}}),
            response({'data': {'createListing': {'__typename': 'CreateListingSuccess', 'listing': {'listingId': '123'}}}}),
            response({'data': {'sellerListings': {'__typename': 'ListingsSuccess', 'listings': [{'__typename': 'ListingSuccess', 'listingId': '123', 'listing': observed(self.item)}]}}})]

    def test_submit_persists_before_execute_and_updates_catalog(self):
        sequence = self.success()
        def respond(*args, **kwargs):
            mode = kwargs['json']['variables'].get('input', {}).get('options', {}).get('operationMode')
            if mode == 'EXECUTE':
                self.assertEqual(self.journal.record['state'], 'submitting')
                self.assertEqual(self.journal.record['listing']['idempotencyKey'], kwargs['json']['variables']['input']['idempotencyKey'])
            return sequence.pop(0)
        self.http.post.side_effect = respond
        receipt = run('secret', self.item, self.journal, self.catalog, self.http, NOW)
        self.assertEqual(receipt['state'], 'verified')
        self.assertEqual(receipt['catalog_state'], 'saved')
        self.assertTrue(receipt['validation']['ok'])
        self.catalog.write_receipt.assert_called_once()
        self.assertNotIn('secret', json.dumps(self.journal.record))

    def test_catalog_failure_retries_only_catalog_never_ebay(self):
        self.http.post.side_effect = self.success()
        self.catalog.write_receipt.side_effect = CatalogError('Connection unavailable')
        result = run('secret', self.item, self.journal, self.catalog, self.http, NOW)
        self.assertEqual(result['catalog_state'], 'pending')
        self.catalog.write_receipt.side_effect = None
        self.http.reset_mock()
        self.http.post.side_effect = [self.success()[-1]]
        result = run('secret', self.item, self.journal, self.catalog, self.http, NOW, reconcile_only=True)
        self.assertEqual(self.http.post.call_count, 1)
        self.assertIn('sellerListings', self.http.post.call_args.kwargs['json']['query'])
        self.assertEqual(result['catalog_state'], 'saved')
        self.http.reset_mock()
        self.catalog.reset_mock()
        run('secret', self.item, self.journal, self.catalog, self.http, NOW, reconcile_only=True)
        self.http.post.assert_not_called()
        self.catalog.write_receipt.assert_not_called()

    def test_timeout_is_durable_and_repeat_only_searches(self):
        self.http.post.side_effect = self.success()[:2] + [requests.Timeout()]
        with self.assertRaises(PublishError): run('secret', self.item, self.journal, self.catalog, self.http, NOW)
        self.assertEqual(self.journal.record['state'], 'unknown')
        key = self.journal.record['listing']['idempotencyKey']
        self.http.reset_mock()
        self.http.post.side_effect = [response(EMPTY)]
        with self.assertRaises(PublishError): run('secret', self.item, self.journal, self.catalog, self.http, NOW)
        self.assertEqual(self.http.post.call_count, 1)
        self.assertEqual(key, self.journal.record['listing']['idempotencyKey'])
        self.catalog.write_receipt.assert_not_called()

    def test_recovery_after_lost_response_links_existing_without_create(self):
        self.http.post.side_effect = self.success()[:2] + [requests.Timeout()]
        with self.assertRaises(PublishError): run('secret', self.item, self.journal, self.catalog, self.http, NOW)
        search = copy.deepcopy(EMPTY)
        search['data']['sellerListingSearch']['scheduledListings'].update(listings=[{'listingId': '123'}], pagination={'totalElements': 1})
        self.http.reset_mock()
        self.http.post.side_effect = [response(search), self.success()[-1]]
        result = run('secret', self.item, self.journal, self.catalog, self.http, NOW, reconcile_only=True)
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(self.http.post.call_count, 2)

    def test_wrong_observed_price_keeps_identifier_but_no_catalog_write(self):
        responses = self.success()
        bad = observed(self.item)
        bad['items'][0]['itemTerms']['pricingTerms']['fixedPrice']['original']['value'] = '1.00'
        responses[-1] = response({'data': {'sellerListings': {'__typename': 'ListingsSuccess', 'listings': [{'__typename': 'ListingSuccess', 'listingId': '123', 'listing': bad}]}}})
        self.http.post.side_effect = responses
        with self.assertRaises(PublishError): run('secret', self.item, self.journal, self.catalog, self.http, NOW)
        self.assertEqual(self.journal.record['listing_id'], '123')
        self.catalog.write_receipt.assert_not_called()

    def test_catalog_block_happens_before_any_ebay_call(self):
        self.catalog.ensure_unlisted.side_effect = CatalogError('Already sold')
        with self.assertRaises(CatalogError): run('secret', self.item, self.journal, self.catalog, self.http, NOW)
        self.http.post.assert_not_called()
        self.assertIsNone(self.journal.record)

    def test_day_required_and_eastern_default_handles_seasons(self):
        with self.assertRaises(PublishError): select_schedule(self.item, '', now=NOW)
        winter = select_schedule(self.item, '2026-11-16', now=NOW)
        summer = select_schedule(self.item, '2027-07-01', now=NOW)
        self.assertEqual(winter['listingTerms']['lifecycleTerms']['scheduledToStartAt'], '2026-11-16T12:00:00Z')
        self.assertEqual(summer['listingTerms']['lifecycleTerms']['scheduledToStartAt'], '2027-07-01T11:00:00Z')
        immediate = select_schedule(self.item, '', now=NOW, immediate=True)
        self.assertNotIn('scheduledToStartAt', immediate['listingTerms']['lifecycleTerms'])

    def test_review_rejects_mismatched_sku_quantity_and_unhosted_images(self):
        for path, value in [('sku', 'OTHER')]:
            bad = copy.deepcopy(self.item); bad[path] = value
            with self.assertRaises(PublishError): review(bad)
        bad = copy.deepcopy(self.item); bad['product']['imageUrls'] = ['https://evil.example/image.jpg']
        with self.assertRaises(PublishError): review(bad)


class CatalogTests(unittest.TestCase):
    def fixture(self):
        header = [''] * 43
        for c, h in HEADERS.items(): header[col_index(c)] = h
        row = [''] * 43
        row[0] = 'MW-2026-999'
        row[col_index('C')] = 'Book'
        row[col_index('AA')] = 'Ready to List'
        return {'valueRanges': [{'values': [header, row]}, {'values': [CHANNEL_HEADERS]}]}

    def test_row_resolution_and_schema_fail_closed(self):
        http = Mock()
        data = self.fixture()
        http.request.return_value = response(data)
        catalog = Catalog(http=http)
        self.assertEqual(catalog.read('MW-2026-999')[0], 3)
        catalog.ensure_unlisted('MW-2026-999')
        data['valueRanges'][0]['values'][0][0] = 'Changed'
        with self.assertRaises(CatalogError): catalog.read('MW-2026-999')

    def test_sold_and_duplicate_catalog_rows_block(self):
        for duplicate in (False, True):
            http = Mock(); data = self.fixture()
            if duplicate: data['valueRanges'][0]['values'].append(data['valueRanges'][0]['values'][1][:])
            else: data['valueRanges'][0]['values'][1][col_index('AN')] = 'Sold'
            http.request.return_value = response(data)
            with self.assertRaises(CatalogError): Catalog(http=http).ensure_unlisted('MW-2026-999')

    def test_receipt_writes_only_explicit_columns_preserves_formulas_and_dates(self):
        http = Mock(); data = self.fixture()
        written = copy.deepcopy(data)
        written['valueRanges'][0]['values'][1][col_index('AA')] = 'Scheduled'
        written['valueRanges'][0]['values'][1][col_index('AN')] = 'Scheduled'
        written['valueRanges'][1]['values'].append(['MW-2026-999', 'eBay', 'Scheduled', '29.95', '', 'https://www.ebay.com/itm/123'])
        http.request.side_effect = [response(data), response({}), response({}), response(written)]
        receipt = {'sku': 'MW-2026-999', 'listing_id': '123', 'observed': observed(item()), 'verified_at': NOW.isoformat(), 'intent_hash': digest(item())}
        Catalog(http=http).write_receipt(receipt)
        updates = http.request.call_args_list[1].kwargs['json']['data']
        columns = {re['range'].split('!')[1].rstrip('0123456789') for re in updates}
        self.assertEqual(columns, {'Z', 'AA', 'AN', 'AB', 'AJ', 'AL', 'AM'})
        self.assertNotIn('AD', columns)
        self.assertEqual(http.request.call_args_list[1].kwargs['json']['valueInputOption'], 'RAW')


class RouteTests(unittest.TestCase):
    def setUp(self):
        web.app.config.update(TESTING=True, SECRET_KEY='test')
        self.client = web.app.test_client()

    def test_public_cannot_access_receipts_or_publish(self):
        for url in ('/sell', '/sell/receipts/MW-2026-999'):
            self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.post('/sell/submit').status_code, 302)

    @patch.dict(os.environ, {'APP_ACCESS_PASSWORD_HASH': generate_password_hash('test-only')})
    def test_setup_missing_blocks_before_ebay(self):
        with self.client.session_transaction() as s:
            s['owner_until'] = time.time() + 1000
            s['workflow_csrf'] = 'csrf'
        with patch('resale_routes.setup_ready', return_value=False), patch('resale_routes.run') as run_mock:
            self.assertEqual(self.client.post('/sell/submit', data={'csrf': 'csrf', 'action': 'now'}).status_code, 503)
            run_mock.assert_not_called()

    @patch.dict(os.environ, {'APP_ACCESS_PASSWORD_HASH': generate_password_hash('test-only')})
    def test_csrf_required_even_for_owner(self):
        with self.client.session_transaction() as s: s['owner_until'] = time.time() + 1000
        self.assertEqual(self.client.post('/sell/submit').status_code, 403)

class PolicyTests(unittest.TestCase):
    def test_calculated_shipping_and_offer_review_has_no_invented_charge(self):
        from listing_terms import selected_policies
        http = Mock()
        http.get.side_effect = [response({'name': 'Media', 'shippingOptions': [{'optionType': 'DOMESTIC', 'costType': 'CALCULATED', 'shippingServices': [{'shippingServiceCode': 'USPSMedia'}]}]}), response({'returnsAccepted': True, 'returnPeriod': {'value': 30, 'unit': 'DAY'}, 'returnShippingCostPayer': 'BUYER'}), response({'name': 'Managed'})]
        terms = selected_policies('secret', item(), http=http)
        self.assertIn('calculated shipping paid by buyer', terms['shipping'][0])
        self.assertIn('30 day', terms['returns'])
        self.assertEqual(len(terms['fingerprint']), 64)
        self.assertNotIn('secret', json.dumps(terms))

class PriceGateRoutesTests(unittest.TestCase):
    def setUp(self):
        web.app.config.update(TESTING=True, SECRET_KEY='test')
        self.client = web.app.test_client()
        web.TOKENS['workflow-test'] = {'access_token': 'secret', 'expires': time.time() + 3600, 'scope': web.LISTING_SCOPE}
        with self.client.session_transaction() as s:
            s['owner_until'] = time.time() + 3600
            s['workflow_csrf'] = 'csrf'
            s['sid'] = 'workflow-test'

    @patch('resale_routes.owner_signed_in', return_value=True)
    @patch('resale_routes.setup_ready', return_value=True)
    @patch('app.configured', return_value=True)
    def test_price_shown_before_any_day_and_submission_cannot_change_payload(self, *mocks):
        policies = {'shipping': ['Calculated Media Mail paid by buyer'], 'returns': '30 day buyer-paid returns', 'names': {'fulfillment': 'Media'}, 'fingerprint': 'a'}
        with patch.dict(os.environ, {'LISTING_DATABASE_URL': 'not-real', 'GOOGLE_SERVICE_ACCOUNT_JSON': '{}', 'EBAY_CLIENT_ID': 'test', 'EBAY_CLIENT_SECRET': 'test', 'EBAY_RUNAME': 'test', 'FLASK_SECRET_KEY': 'test'}), patch('resale_routes.Catalog') as catalog, patch('resale_routes.selected_policies', return_value=policies), patch('resale_routes.run') as submit:
            packet = json.dumps({'format': 'crece-ebay-listing-v1', 'listing': item()}).encode()
            page = self.client.post('/sell', data={'csrf': 'csrf', 'package': (__import__('io').BytesIO(packet), 'listing.json')}, content_type='multipart/form-data')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'$29.95', page.data)
            self.assertIn(b'7 a.m. Eastern', page.data)
            self.assertIn(b'Automatic offer acceptance', page.data)
            self.assertNotIn(b'input type="date" name="day" value=', page.data)
            submit.assert_not_called()
            # Posting without a day fails before execution, even though price was shown.
            page = self.client.post('/sell/submit', data={'csrf': 'csrf', 'action': 'schedule'})
            self.assertEqual(page.status_code, 409)
            self.assertIn(b'Choose a publication day', page.data)
            submit.assert_not_called()
