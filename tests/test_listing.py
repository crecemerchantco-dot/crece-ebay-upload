import copy
import io
import json
import time
import unittest
import zipfile
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import requests
import app as web
from ebay_listing import LISTING_SCOPE, load_package, load_upload, validate_listing


def package():
    return {'format': 'crece-ebay-listing-v1', 'listing': {
        'marketplace': 'EBAY_US', 'sku': 'TEST-UNLISTED-001',
        'product': {'title': 'Unlisted test item'},
        'listingTerms': {'listingFormat': 'FIXED_PRICE'},
        'listingItems': {'items': [{'itemTerms': {}}]},
        'options': {'operationMode': 'EXECUTE'}}}


class ListingTest(unittest.TestCase):
    def test_zip_keeps_photo_order_without_extracting_files(self):
        p = package()
        p['photos'] = ['photos/02.jpg', 'photos/01.jpg']
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('listing.json', json.dumps(p))
            archive.writestr('photos/01.jpg', b'one')
            archive.writestr('photos/02.jpg', b'two')
        prepared, photos = load_upload(stream.getvalue())
        self.assertEqual(photos, [('02.jpg', b'two'), ('01.jpg', b'one')])
        self.assertEqual(prepared['options'], {'operationMode': 'VALIDATE'})

    def test_zip_rejects_traversal_and_unexpected_files(self):
        for name in ['../outside.jpg', 'photos/01.jpg']:
            p = package()
            p['photos'] = [name]
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('listing.json', json.dumps(p))
                archive.writestr(name, b'photo')
                archive.writestr('unexpected.txt', b'extra')
            with self.assertRaises(ValueError):
                load_upload(stream.getvalue())

    def test_execution_mode_and_key_are_replaced_without_mutating_package(self):
        original = package()
        item = load_package(json.dumps(original).encode())
        self.assertEqual(item['options']['operationMode'], 'VALIDATE')
        self.assertRegex(item['idempotencyKey'], r'^[0-9a-f]{32}$')
        self.assertEqual(original['listing']['options']['operationMode'], 'EXECUTE')

    def test_already_listed_scale_cannot_be_the_test(self):
        p = package()
        p['listing']['sku'] = 'MW-2026-200'
        with self.assertRaises(ValueError):
            load_package(json.dumps(p).encode())

    def test_nonpackages_and_oversized_files_are_rejected(self):
        for value in (b'{}', b'not json', b'[]', b'x' * 262145):
            with self.subTest(value=value[:10]), self.assertRaises(ValueError):
                load_package(value)

    def test_server_forces_validation_and_disables_redirects(self):
        http = Mock()
        http.post.return_value = Mock(status_code=200, json=lambda: {'data': {'createListing': {
            '__typename': 'CreateListingValidationSuccess', 'warnings': []}}})
        result = validate_listing('secret', package()['listing'], http)
        self.assertTrue(result['ok'])
        self.assertFalse(result['published'])
        call = http.post.call_args
        self.assertEqual(call.args[0], 'https://graphqlapi.ebay.com/graphql')
        self.assertEqual(call.kwargs['json']['variables']['input']['options'], {'operationMode': 'VALIDATE'})
        self.assertFalse(call.kwargs['allow_redirects'])

    def test_http_success_does_not_hide_graphql_errors(self):
        http = Mock()
        http.post.return_value = Mock(status_code=200, json=lambda: {'errors': [{'message': 'SECRET'}]})
        result = validate_listing('SECRET', {}, http)
        self.assertFalse(result['ok'])
        self.assertNotIn('SECRET', str(result))

    def test_business_errors_keep_field_paths_and_warnings(self):
        http = Mock()
        http.post.return_value = Mock(status_code=200, json=lambda: {'data': {'createListing': {
            '__typename': 'CreateListingValidationError', 'errorCode': 'INVALID', 'errorMessage': 'Fix listing',
            'violations': [{'violationCode': 'MISSING', 'violationDescription': 'Missing title', 'inputPath': ['product', 'title']}],
            'warnings': [{'warningCode': 'WARN', 'warningMessage': 'Check photos'}]}}})
        result = validate_listing('secret', {}, http)
        self.assertFalse(result['ok'])
        self.assertIn('product.title', str(result['errors']))
        self.assertIn('Check photos', str(result['warnings']))

    def test_unexpected_execution_success_is_not_validation_success(self):
        http = Mock()
        http.post.return_value = Mock(status_code=200, json=lambda: {'data': {'createListing': {'__typename': 'CreateListingSuccess'}}})
        self.assertFalse(validate_listing('secret', {}, http)['ok'])

    def test_network_failure_is_not_retried_or_reported_as_passed(self):
        http = Mock()
        http.post.side_effect = requests.Timeout('SECRET')
        result = validate_listing('SECRET', {}, http)
        self.assertFalse(result['ok'])
        self.assertNotIn('SECRET', str(result))
        self.assertEqual(http.post.call_count, 1)


class ListingRoutesTest(unittest.TestCase):
    def setUp(self):
        web.app.config.update(TESTING=True, SECRET_KEY='test-only')
        web.TOKENS.clear()
        self.client = web.app.test_client()

    def authorize(self):
        with self.client.session_transaction() as s:
            s.update(sid='seller', validation_csrf='csrf')
        web.TOKENS['seller'] = {'access_token': 'SECRET', 'expires': time.time() + 3600, 'scope': LISTING_SCOPE}

    @patch('app.configured', return_value=True)
    @patch.dict('os.environ', {'EBAY_CLIENT_ID': 'client', 'EBAY_RUNAME': 'redirect'})
    def test_listing_permission_is_requested_only_by_explicit_connection(self, configured):
        for path in ('/login', '/login?checks=1'):
            scope = parse_qs(urlsplit(self.client.get(path).location).query)['scope'][0]
            self.assertNotIn(LISTING_SCOPE, scope)
        scope = parse_qs(urlsplit(self.client.get('/login?validation=1').location).query)['scope'][0]
        self.assertIn(LISTING_SCOPE, scope)

    @patch('app.configured', return_value=True)
    def test_validation_requires_listing_grant_and_csrf(self, configured):
        self.assertEqual(self.client.post('/validate').status_code, 401)
        self.authorize()
        self.assertEqual(self.client.post('/validate').status_code, 403)

    @patch('app.configured', return_value=True)
    @patch('app.validate_listing')
    def test_uploaded_package_is_sent_to_validator_and_html_is_escaped(self, validator, configured):
        self.authorize()
        validator.return_value = {'ok': False, 'errors': ['<script>bad</script>'], 'warnings': []}
        response = self.client.post('/validate', data={'csrf': 'csrf',
            'package': (io.BytesIO(json.dumps(package()).encode()), 'item.json')})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<script>bad</script>', response.get_data(as_text=True))
        self.assertNotIn('SECRET', response.get_data(as_text=True))
        self.assertEqual(validator.call_args.args[1]['options']['operationMode'], 'VALIDATE')

    @patch('app.configured', return_value=True)
    @patch('app.upload_image_from_url')
    @patch('app.validate_listing')
    def test_failed_photo_upload_stops_before_listing_validation(self, validator, uploader, configured):
        self.authorize()
        p = package()
        p['photos'] = ['photos/01.jpg']
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('listing.json', json.dumps(p))
            archive.writestr('photos/01.jpg', b'photo')
        uploader.return_value = {'url': None}
        response = self.client.post('/validate', data={'csrf': 'csrf', 'package': (io.BytesIO(stream.getvalue()), 'item.zip')})
        self.assertEqual(response.status_code, 502)
        validator.assert_not_called()

    def test_download_never_exposes_another_session(self):
        self.authorize()
        web.TOKENS['other'] = {'prepared_listing': {'sku': 'OTHER'}}
        self.assertEqual(self.client.get('/validate/package').status_code, 404)


if __name__ == '__main__':
    unittest.main()
