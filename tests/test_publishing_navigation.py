import unittest
from unittest.mock import patch
from app import app
from approved_sales import load_approval


class PublishingNavigationTests(unittest.TestCase):
    def test_unconfigured_workspace_keeps_approved_route_visible(self):
        with patch.dict('os.environ', {}, clear=True):
            client = app.test_client()
            home = client.get('/').get_data(as_text=True)
            self.assertIn('/publish/approved', home)
            self.assertNotIn('Open my resale workspace', home)
            fallback = client.get('/owner/login').get_data(as_text=True)
            self.assertIn('/publish/approved', fallback)
            self.assertNotIn('Publishing here is disabled', fallback)

    def test_book_review_is_available_but_cannot_submit_without_seller(self):
        with patch.dict('os.environ', {}, clear=True):
            client = app.test_client()
            response = client.get('/publish/approved/mcgee-293')
            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
            self.assertIn('Friday, November 20, 2026 at 7 a.m. Eastern', text)
            self.assertIn('$12.95', text)
            self.assertIn('USPS Media Mail', text)
            self.assertNotIn('Schedule the approved item</button>', text)
            self.assertEqual(client.post('/publish/approved/mcgee-293').status_code, 401)
        record = load_approval('mcgee-293')
        self.assertEqual(len(record['listing']['product']['imageUrls']), 10)

