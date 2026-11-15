import copy
import time
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import requests
import app as web
from ebay_checks import CheckError, EbayChecks, preflight, ACCOUNT_SCOPE


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.category = {'categoryId': '163098', 'aspects': [
            {'localizedAspectName': 'Maker', 'aspectConstraint': {'aspectRequired': True}}]}
        self.item = {'sku': 'MW-TEST-001', 'title': 'Test scale', 'description': 'Untested scale',
                     'price': '34.95', 'quantity': 1, 'categoryId': '163098',
                     'imageUrls': ['https://i.ebayimg.com/example.jpg'],
                     'aspects': {'Maker': 'American Cutlery Co.'},
                     'scheduleTime': '2099-11-15T12:00:00Z'}

    def test_preflight_never_claims_publication_or_ebay_acceptance(self):
        r = preflight(self.item, self.category)
        self.assertTrue(r['ok'])
        self.assertFalse(r['published'])
        self.assertEqual(r['state'], 'local_preflight_only')

    def test_required_aspect_and_invalid_price(self):
        self.item.update(price='NaN', aspects={})
        r = preflight(self.item, self.category)
        self.assertFalse(r['ok'])
        self.assertEqual(len(r['errors']), 2)

    def test_bad_photo_and_expired_schedule(self):
        self.item.update(imageUrls=['https://i.ebayimg.com@evil.test/a.jpg'],
                         scheduleTime='2001-01-01T00:00:00Z')
        self.assertFalse(preflight(self.item, self.category)['ok'])

    def test_selection_only_values(self):
        self.category['aspects'][0].update(
            aspectConstraint={'aspectRequired': True, 'aspectMode': 'SELECTION_ONLY'},
            aspectValues=[{'localizedValue': 'Fairbanks'}])
        self.assertFalse(preflight(self.item, self.category)['ok'])

    def test_category_rejects_path_injection_without_http(self):
        http = Mock()
        with self.assertRaises(ValueError):
            EbayChecks('seller', 'app', http).category('../account')
        http.get.assert_not_called()

    def test_taxonomy_discovers_current_tree_and_uses_application_token(self):
        http = Mock()
        http.get.side_effect = [Mock(status_code=200, json=lambda: {'categoryTreeId': '0'}),
                                Mock(status_code=200, json=lambda: {'aspects': []})]
        self.assertEqual(EbayChecks('seller', 'app', http).category('163098')['categoryTreeId'], '0')
        for call in http.get.call_args_list:
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer app')
            self.assertFalse(call.kwargs['allow_redirects'])
        http.post.assert_not_called()

    def test_api_errors_do_not_echo_tokens_or_response_bodies(self):
        http = Mock()
        http.get.return_value = Mock(status_code=403, text='SECRET_TOKEN')
        r = EbayChecks('SECRET_TOKEN', http=http).policies()
        self.assertNotIn('SECRET_TOKEN', str(r))
        self.assertTrue(all(not x['ok'] for x in r.values()))

    def test_timeout_is_partial_check_failure_not_empty_success(self):
        http = Mock()
        http.get.side_effect = requests.Timeout('SECRET')
        self.assertFalse(EbayChecks('token', http=http).policies()['payment']['ok'])


class RoutesTest(unittest.TestCase):
    def setUp(self):
        web.app.config.update(TESTING=True, SECRET_KEY='test-only')
        web.TOKENS.clear()
        self.client = web.app.test_client()

    def test_policies_require_seller_session(self):
        self.assertEqual(self.client.get('/checks/policies').status_code, 401)

    def test_preflight_requires_csrf(self):
        with self.client.session_transaction() as s:
            s['sid'] = 'test'
        web.TOKENS['test'] = {'access_token': 'private', 'expires': time.time() + 3600}
        self.assertEqual(self.client.post('/checks/preflight', data={}).status_code, 403)

    @patch('app.configured', return_value=True)
    @patch.dict('os.environ', {'EBAY_CLIENT_ID': 'test-client', 'EBAY_RUNAME': 'test-redirect'})
    def test_account_scope_only_requested_by_explicit_checks_connection(self, _):
        normal = parse_qs(urlsplit(self.client.get('/login').location).query)['scope'][0]
        checked = parse_qs(urlsplit(self.client.get('/login?checks=1').location).query)['scope'][0]
        self.assertNotIn(ACCOUNT_SCOPE, normal)
        self.assertIn(ACCOUNT_SCOPE, checked)

    @patch('app.requests.post')
    @patch.dict('os.environ', {'EBAY_CLIENT_ID': 'test-client', 'EBAY_CLIENT_SECRET': 'private'})
    def test_refresh_does_not_expand_existing_grant(self, post):
        post.return_value = Mock(json=lambda: {'access_token': 'new-private', 'expires_in': 7200})
        with self.client.session_transaction() as s:
            s['sid'] = 'test'
        web.TOKENS['test'] = {'access_token': 'old', 'expires': 0, 'refresh_token': 'private'}
        with web.app.test_request_context():
            web.session['sid'] = 'test'
            self.assertEqual(web.access_token(), 'new-private')
        self.assertNotIn('scope', post.call_args.kwargs['data'])

    @patch('app.EbayChecks')
    def test_policy_html_escapes_names_and_never_renders_token(self, checks):
        with self.client.session_transaction() as s:
            s['sid'] = 'test'
        web.TOKENS['test'] = {'access_token': 'private-token', 'expires': time.time() + 3600}
        checks.return_value.policies.return_value = {'payment': {'ok': True, 'policies': [
            {'name': '<script>bad</script>', 'id': '1'}]}}
        html = self.client.get('/checks/policies').get_data(as_text=True)
        self.assertNotIn('<script>bad</script>', html)
        self.assertNotIn('private-token', html)
        self.assertIn('&lt;script&gt;', html)


if __name__ == '__main__':
    unittest.main()
