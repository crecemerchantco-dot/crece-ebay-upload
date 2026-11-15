import copy
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests
import app as web
from ebay_listing import LISTING_SCOPE
from ebay_publish import approval, schedule_approved_book, PublishError


EMPTY = {'data': {'sellerListingSearch': {field: {'__typename': 'SellerListings', 'listings': [], 'pagination': {'totalElements': 0}} for field in ('activeListings', 'scheduledListings')}}}
def response(payload):
    return Mock(status_code=200, json=lambda: payload)

class PublishTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        self.http = Mock()

    def test_full_flow_checks_duplicates_validates_executes_once_then_verifies(self):
        self.http.post.side_effect = [response(EMPTY), response({'data': {'createListing': {'__typename': 'CreateListingValidationSuccess'}}}),
            response({'data': {'createListing': {'__typename': 'CreateListingSuccess', 'listing': {'listingId': '123'}}}}),
            response({'data': {'sellerListings': {'__typename': 'ListingsSuccess', 'listings': [{'__typename': 'ListingSuccess', 'listingId': '123', 'listing': {'listingTerms': {'lifecycleTerms': {'listingStatus': 'SCHEDULED', 'startAt': '2026-11-16T12:00:00Z'}}}}]}}})]
        result = schedule_approved_book('secret', self.http, self.now)
        self.assertEqual(result['listing_id'], '123')
        self.assertEqual(self.http.post.call_count, 4)
        calls = self.http.post.call_args_list
        self.assertEqual(calls[1].kwargs['json']['variables']['input']['options'], {'operationMode': 'VALIDATE'})
        created = calls[2].kwargs['json']['variables']['input']
        self.assertEqual(created, approval()['listing'])
        self.assertFalse(calls[2].kwargs['allow_redirects'])

    def test_existing_sku_never_creates_a_second_listing(self):
        found = copy.deepcopy(EMPTY)
        found['data']['sellerListingSearch']['scheduledListings'].update(listings=[{'listingId': '456'}], pagination={'totalElements': 1})
        self.http.post.side_effect = [response(found), response({'data': {'sellerListings': {'__typename': 'ListingsSuccess', 'listings': [{'__typename': 'ListingSuccess', 'listingId': '456', 'listing': {}}]}}})]
        self.assertFalse(schedule_approved_book('secret', self.http, self.now)['created'])
        self.assertEqual(self.http.post.call_count, 2)

    def test_incomplete_duplicate_search_blocks_creation(self):
        found = copy.deepcopy(EMPTY)
        found['data']['sellerListingSearch']['activeListings']['pagination']['nextCursor'] = 'more'
        self.http.post.return_value = response(found)
        with self.assertRaises(PublishError): schedule_approved_book('secret', self.http, self.now)
        self.assertEqual(self.http.post.call_count, 1)

    def test_validation_failure_never_executes(self):
        self.http.post.side_effect = [response(EMPTY), response({'data': {'createListing': {'__typename': 'CreateListingValidationError', 'errorCode': 'BAD', 'errorMessage': 'Fix it'}}})]
        with self.assertRaises(PublishError): schedule_approved_book('secret', self.http, self.now)
        self.assertEqual(self.http.post.call_count, 2)

    def test_create_timeout_is_unknown_and_not_retried(self):
        self.http.post.side_effect = [response(EMPTY), response({'data': {'createListing': {'__typename': 'CreateListingValidationSuccess'}}}), requests.Timeout('secret')]
        with self.assertRaises(PublishError) as error: schedule_approved_book('secret', self.http, self.now)
        self.assertIn('unknown', str(error.exception))
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(self.http.post.call_count, 3)

    def test_expired_schedule_cannot_turn_into_immediate_publication(self):
        self.http.post.return_value = response(EMPTY)
        with self.assertRaises(PublishError): schedule_approved_book('secret', self.http, datetime(2026,11,17,tzinfo=timezone.utc))
        self.assertEqual(self.http.post.call_count, 1)

    def test_approved_key_survives_repeated_loads(self):
        self.assertEqual(approval()['listing']['idempotencyKey'], approval()['listing']['idempotencyKey'])

class PublishRoutesTests(unittest.TestCase):
    def setUp(self):
        web.app.config.update(TESTING=True, SECRET_KEY='test')
        web.TOKENS.clear()
        self.client = web.app.test_client()

    @patch('app.configured', return_value=True)
    @patch('app.schedule_approved_book')
    def test_publication_requires_seller_token_and_csrf(self, submit, configured):
        self.assertEqual(self.client.post('/publish/kitcho').status_code, 401)
        with self.client.session_transaction() as s: s['sid'] = 'seller'
        web.TOKENS['seller'] = {'access_token': 'secret', 'expires': time.time()+3600, 'scope': LISTING_SCOPE}
        self.assertEqual(self.client.post('/publish/kitcho').status_code, 403)
        submit.assert_not_called()

    @patch('app.configured', return_value=True)
    @patch('app.schedule_approved_book')
    def test_get_page_never_publishes(self, submit, configured):
        self.assertEqual(self.client.get('/publish/kitcho').status_code, 200)
        submit.assert_not_called()

if __name__ == '__main__': unittest.main()
