import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from approved_sales import load_approval, submit
from ebay_publish import PublishError

class ApprovedSaleTests(unittest.TestCase):
    def test_only_frozen_registered_item_is_accepted(self):
        with self.assertRaises(PublishError): load_approval('../kitcho')
        r = load_approval('mug-321')
        self.assertEqual(r['listing']['sku'], 'MW-2026-321')
        self.assertEqual(r['listing']['listingItems']['items'][0]['itemTerms']['pricingTerms']['fixedPrice'], '8.95')
        self.assertEqual(r['listing']['listingTerms']['lifecycleTerms']['scheduledToStartAt'], '2026-11-17T12:00:00Z')

    def test_creation_key_never_changes_between_loads(self):
        self.assertEqual(load_approval('mug-321')['listing']['idempotencyKey'], load_approval('mug-321')['listing']['idempotencyKey'])

    @patch('approved_sales.find_existing', return_value=['123'])
    @patch('approved_sales.verify', return_value={})
    @patch('approved_sales.check_observed')
    @patch('approved_sales.post')
    def test_existing_listing_never_executes(self, post, check, verify, find):
        r = submit('mug-321', 'secret', Mock())
        self.assertFalse(r['created'])
        post.assert_not_called()

    @patch('approved_sales.find_existing', return_value=[])
    @patch('approved_sales.selected_policies')
    def test_expired_schedule_never_submits(self, policies, find):
        with self.assertRaises(PublishError): submit('mug-321', 'secret', Mock(), datetime(2026,11,18,tzinfo=timezone.utc))
        policies.assert_not_called()

    @patch('approved_sales.find_existing', return_value=[])
    @patch('approved_sales.selected_policies', return_value={'shipping':['Domestic: USPSGroundAdvantage — calculated shipping paid by buyer','Handling: 2 day'],'returns':'30 day returns; return shipping paid by buyer'})
    @patch('approved_sales.validate_listing', return_value={'ok':True,'errors':[],'warnings':[]})
    @patch('approved_sales.post', return_value={'createListing': {'__typename':'CreateListingSuccess','listing':{'listingId':'456'}}})
    @patch('approved_sales.verify', side_effect=PublishError('Verification unavailable'))
    def test_accepted_id_survives_unavailable_verification(self, verify, post, validation, policies, find):
        r = submit('mug-321','secret',Mock(),datetime(2026,11,15,tzinfo=timezone.utc))
        self.assertEqual(r['listing_id'],'456')
        self.assertIsNone(r['observed'])
        payload = post.call_args.args[2]['input']
        self.assertEqual(payload,load_approval('mug-321')['listing'])
        self.assertEqual(post.call_count,1)
