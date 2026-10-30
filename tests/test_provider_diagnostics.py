import os
import unittest
from unittest.mock import Mock, patch

from app.services.whatsapp_service import get_meta_connection_status


class ProviderDiagnosticsTests(unittest.TestCase):
    def test_360dialog_checks_its_own_credentials_without_testing_meta(self):
        response = Mock(status_code=200, text='{"waba_templates": []}')
        response.json.return_value = {'waba_templates': []}
        with patch.dict(os.environ, {'WHATSAPP_PROVIDER': '360dialog', 'D360_API_KEY': 'synthetic-key',
                'D360_API_BASE_URL': 'https://waba-v2.360dialog.io',
                'D360_TEMPLATE_API_URL': 'https://waba-v2.360dialog.io/v1/configs/templates'}), patch(
                'app.services.whatsapp_service.requests.get', return_value=response) as get:
            result = get_meta_connection_status()
        self.assertTrue(result.ok)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].args[0], 'https://waba-v2.360dialog.io/v1/configs/templates')
        self.assertEqual(get.call_args.args[0], 'https://waba-v2.360dialog.io/whatsapp_business_profile')
        self.assertEqual(get.call_args.kwargs['headers']['D360-API-KEY'], 'synthetic-key')
        self.assertIn('sending is not tested', result.diagnostics[0]['name'])
        self.assertNotIn('synthetic-key', str(result))

    def test_360dialog_missing_key_fails_without_falling_back_to_meta(self):
        with patch.dict(os.environ, {'WHATSAPP_PROVIDER': '360dialog', 'D360_API_KEY': ''}), patch(
                'app.services.whatsapp_service.requests.get') as get:
            result = get_meta_connection_status()
        self.assertFalse(result.ok)
        self.assertIn('D360_API_KEY', result.error)
        get.assert_not_called()

    def test_360dialog_failure_reports_http_code_and_trace(self):
        data = {'error': {'message': 'Unsupported sender', 'code': 100, 'fbtrace_id': 'test-trace'}}
        response = Mock(status_code=400, text='provider error')
        response.json.return_value = data
        with patch.dict(os.environ, {'WHATSAPP_PROVIDER': '360dialog', 'D360_API_KEY': 'synthetic-key'}), patch(
                'app.services.whatsapp_service.requests.get', return_value=response):
            result = get_meta_connection_status()
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0]['status_code'], 400)
        self.assertIn('code 100', result.error)
        self.assertIn('test-trace', result.error)

    def test_template_access_does_not_hide_broken_sender_access(self):
        templates = Mock(status_code=200, text='{}')
        templates.json.return_value = {'waba_templates': []}
        profile = Mock(status_code=400, text='{}')
        profile.json.return_value = {'error': {'message': 'Sender inaccessible', 'code': 100, 'error_subcode': 33}}
        with patch.dict(os.environ, {'WHATSAPP_PROVIDER': '360dialog', 'D360_API_KEY': 'synthetic-key'}), patch(
                'app.services.whatsapp_service.requests.get', side_effect=[templates, profile]):
            result = get_meta_connection_status()
        self.assertFalse(result.ok)
        self.assertTrue(result.diagnostics[0]['ok'])
        self.assertFalse(result.diagnostics[1]['ok'])
        self.assertIn('subcode 33', result.error)

