import io
import json
import unittest
from datetime import datetime, timedelta, date
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_analytics_security as security_fixture
from app import db
from app.models import (LapsedPolicy, MarketingScreening, SystemSetting, CommunicationCampaign,
                        CampaignRecipient, CommunicationEvent, ClientApplication, ContactSuppression)
from app.services.client_directory import filter_policy_query, scoped_policies
from app.services.marketing_screening import clearance, make_evidence, result_status
from app.services.communication_service import preference_for


class MarketingScreeningTests(unittest.TestCase):
    def setUp(self):
        security_fixture.AnalyticsSecurityTests.setUp(self)
        db.session().expire_on_commit = False

    tearDown = security_fixture.AnalyticsSecurityTests.tearDown
    login = security_fixture.AnalyticsSecurityTests.login

    def lead(self, branch='A'):
        row = LapsedPolicy.query.filter_by(policy_number=branch).one()
        row.cell_number = '0821234567' if branch == 'A' else '0827654321'
        row.franchise = 'Northcliff' if branch == 'A' else 'Southridge'
        row.last_date_paid = date(2023, 6, 22)
        db.session.commit()
        return row

    def evidence(self, lead, registry='dmasa', result='clear', checked=None, expires=None):
        checked = checked or datetime.utcnow() - timedelta(minutes=2)
        expires = expires or datetime.utcnow() + timedelta(days=1)
        row = make_evidence(lead, registry, result, checked, expires, 'Synthetic test report', self.user_ids['admin'])
        db.session.add(row)
        db.session.commit()
        return row

    def configure(self, required=None):
        row = SystemSetting.query.filter_by(category='MarketingCompliance', key='registry_policy').first()
        if not row:
            row = SystemSetting(category='MarketingCompliance', key='registry_policy')
            db.session.add(row)
        row.value = json.dumps({'required': ['dmasa'] if required is None else required, 'reference': 'Test scope review'})
        db.session.commit()

    def token(self, name='admin'):
        self.login(name)
        self.assertEqual(self.client.get('/marketing-screening/').status_code, 200)
        with self.client.session_transaction() as session:
            return session['screening_csrf']

    def campaign(self, lead):
        campaign = CommunicationCampaign(name='Test only', message_body='Synthetic', created_by_id=self.user_ids['admin'],
            whatsapp_template_name='test', image_url='https://example.test/image.png', template_status='Approved',
            status='Scheduled', scheduled_at=datetime.utcnow() - timedelta(minutes=1), queue_status='queued')
        db.session.add(campaign)
        db.session.flush()
        recipient = CampaignRecipient(campaign_id=campaign.id, lapsed_policy_id=lead.id, secure_token='synthetic-token')
        db.session.add(recipient)
        db.session.commit()
        return campaign, recipient

    def test_default_preferences_do_not_establish_consent(self):
        lead = self.lead()
        self.configure()
        self.evidence(lead)
        self.assertTrue(preference_for(lead).whatsapp_allowed)
        self.assertIn('consent', clearance(lead)[1])
        self.assertFalse(clearance(lead)[0])

    def test_missing_policy_checks_and_expired_checks_hold_then_clear(self):
        lead = self.lead()
        self.evidence(lead, 'whatsapp', 'granted')
        self.assertIn('Administrator', clearance(lead)[1])
        self.configure()
        self.assertIn('Not checked', clearance(lead)[1])
        self.evidence(lead, checked=datetime.utcnow()-timedelta(days=3), expires=datetime.utcnow()-timedelta(days=1))
        self.assertIn('Expired', clearance(lead)[1])
        self.evidence(lead)
        self.assertTrue(clearance(lead)[0])

    def test_known_listing_blocks_even_nonrequired_and_failed_recheck(self):
        lead = self.lead()
        self.configure([])
        self.evidence(lead, 'whatsapp', 'granted')
        self.evidence(lead, 'waspa', 'listed', checked=datetime.utcnow()-timedelta(days=300))
        self.evidence(lead, 'waspa', 'error')
        self.assertIn('Listed on WASPA', clearance(lead)[1])
        with self.app.test_request_context():
            self.assertEqual(filter_policy_query(LapsedPolicy.query, {'registry':'waspa', 'screening':'listed'}).count(), 1)
        self.evidence(lead, 'waspa', 'clear', checked=datetime.utcnow()-timedelta(seconds=1))
        self.assertTrue(clearance(lead)[0])

    def test_changed_number_invalidates_registry_and_consent_evidence(self):
        lead = self.lead()
        self.configure()
        self.evidence(lead)
        self.evidence(lead, 'whatsapp', 'granted')
        lead.cell_number = '0821112233'
        db.session.commit()
        self.assertFalse(clearance(lead)[0])
        self.login('admin')
        with self.app.test_request_context():
            query = filter_policy_query(LapsedPolicy.query, {'registry':'dmasa','screening':'not_checked'})
            self.assertIn(lead.id, [p.id for p in query])

    def test_equivalent_formats_and_shared_number_use_the_same_check(self):
        lead = self.lead()
        other = self.lead('B')
        self.evidence(lead, result='listed')
        other.cell_number = '+27 (82) 123-4567'
        db.session.commit()
        self.login('admin')
        with self.app.test_request_context():
            rows = filter_policy_query(LapsedPolicy.query, {'registry':'dmasa','screening':'listed'}).all()
            self.assertEqual({p.id for p in rows}, {lead.id, other.id})
        self.assertIn('Listed', clearance(other)[1])

    def test_latest_result_and_register_filters_combine_with_franchise_and_dates(self):
        lead, other = self.lead(), self.lead('B')
        self.evidence(lead, result='listed', checked=datetime.utcnow()-timedelta(days=3))
        self.evidence(lead)
        self.evidence(other, 'ncc', 'listed')
        self.login('admin')
        with self.app.test_request_context():
            filters = {'registry':'dmasa','screening':'clear','franchise':'Northcliff','year':'2023','month':'6'}
            self.assertEqual([p.id for p in filter_policy_query(LapsedPolicy.query, filters)], [lead.id])
            self.assertEqual([p.id for p in filter_policy_query(LapsedPolicy.query, {'screening':'listed'})], [other.id])
        response = self.client.get('/client-files/?registry=ncc&screening=listed')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Southridge', response.data)
        self.assertIn(b'Listed', response.data)
        self.assertEqual(self.client.get('/client-files/?registry=invalid').status_code, 400)

    def test_expired_error_and_unchecked_filters(self):
        lead = self.lead()
        self.evidence(lead, checked=datetime.utcnow()-timedelta(days=3), expires=datetime.utcnow()-timedelta(days=1))
        self.login('admin')
        with self.app.test_request_context():
            self.assertEqual(filter_policy_query(LapsedPolicy.query, {'registry':'dmasa','screening':'expired'}).count(), 1)
            self.assertEqual(filter_policy_query(LapsedPolicy.query, {'registry':'ncc','screening':'not_checked'}).count(), 2)
        self.evidence(lead, result='error')
        with self.app.test_request_context():
            self.assertEqual(filter_policy_query(LapsedPolicy.query, {'registry':'dmasa','screening':'error'}).count(), 1)

    def test_roles_scope_and_csrf_protect_history_and_changes(self):
        lead, other = self.lead(), self.lead('B')
        token = self.token('manager')
        self.assertEqual(self.client.get(f'/marketing-screening/client/{other.id}').status_code, 404)
        self.assertEqual(self.client.post('/marketing-screening/policy', data={'screening_csrf':token}).status_code, 403)
        self.assertEqual(self.client.post(f'/marketing-screening/client/{lead.id}', data={}).status_code, 400)
        token = self.token('agent')
        self.assertEqual(self.client.post(f'/marketing-screening/client/{lead.id}', data={'screening_csrf':token}).status_code, 403)

    def test_recording_is_append_only_requires_reference_and_validity(self):
        lead = self.lead()
        token = self.token()
        data = {'screening_csrf':token, 'registry':'dmasa', 'result':'clear', 'checked_at':'2023-06-22T12:00',
                'reference':'Test provider report'}
        self.client.post(f'/marketing-screening/client/{lead.id}', data=data)
        self.assertEqual(MarketingScreening.query.count(), 0)
        data['valid_until'] = '2023-07-22T12:00'
        self.client.post(f'/marketing-screening/client/{lead.id}', data=data)
        self.assertEqual(MarketingScreening.query.one().checked_at, datetime(2023,6,22,10))
        data.update(result='listed', checked_at='2023-06-23T12:00')
        self.client.post(f'/marketing-screening/client/{lead.id}', data=data)
        self.assertEqual(MarketingScreening.query.count(), 2)
        response = self.client.get(f'/marketing-screening/client/{lead.id}')
        self.assertIn(b'Test provider report', response.data)
        self.assertIn(b'2023-06-23 12:00', response.data)

    def test_csv_import_atomic_scoped_and_never_sends(self):
        lead, other = self.lead(), self.lead('B')
        token = self.token('manager')
        header = 'phone_number,result,checked_at,valid_until,reference\n'
        good = '0821234567,listed,2023-06-22T12:00,,Test batch\n'
        bad = '0827654321,listed,2023-06-22T12:00,,Other branch\n'
        def upload(content):
            return self.client.post('/marketing-screening/import', data={'screening_csrf':token,'registry':'dmasa',
                'file':(io.BytesIO(content.encode()), 'results.csv')})
        with patch('app.routes.communications.send_whatsapp_template_image') as sender:
            self.assertEqual(upload(header+good+bad).status_code, 302)
            self.assertEqual(MarketingScreening.query.count(), 0)
            self.assertEqual(upload(header+good).status_code, 302)
            self.assertEqual(MarketingScreening.query.one().lapsed_policy_id, lead.id)
            sender.assert_not_called()

    def test_admin_review_explicitly_confirms_no_register_applicable(self):
        token = self.token()
        data = {'screening_csrf':token, 'reference':'Reviewed provider scope'}
        self.assertEqual(self.client.post('/marketing-screening/policy', data=data).status_code, 400)
        data['confirm_none'] = 'yes'
        self.assertEqual(self.client.post('/marketing-screening/policy', data=data).status_code, 302)
        from app.services.marketing_screening import registry_policy
        self.assertEqual(registry_policy()['required'], [])

    def test_common_sender_holds_before_provider_and_allows_only_with_evidence(self):
        from app.routes.communications import _send_to_recipient
        lead = self.lead()
        campaign, recipient = self.campaign(lead)
        with self.app.test_request_context(), patch('app.routes.communications.send_whatsapp_template_image',
                return_value=SimpleNamespace(ok=False, error='Synthetic provider failure', message_id=None, response_json={})) as sender:
            self.assertFalse(_send_to_recipient(campaign, recipient, 'whatsapp')[0])
            self.assertEqual(recipient.whatsapp_status, 'Held')
            sender.assert_not_called()
            self.configure()
            self.evidence(lead)
            self.evidence(lead, 'whatsapp', 'granted')
            _send_to_recipient(campaign, recipient, 'whatsapp')
            sender.assert_called_once()

    def test_scheduled_campaign_held_is_not_reported_sent_and_can_be_requeued(self):
        from app.services.whatsapp_campaign_engine import process_scheduled_campaigns
        lead = self.lead()
        campaign, recipient = self.campaign(lead)
        with self.app.test_request_context(), patch('app.routes.communications._refresh_template_status',
                return_value=SimpleNamespace(ok=True)), patch('app.routes.communications.send_whatsapp_template_image') as sender:
            process_scheduled_campaigns()
            self.assertEqual(campaign.status, 'Needs attention')
            self.assertEqual(campaign.queue_status, 'held')
            self.assertIsNone(campaign.sent_at)
            sender.assert_not_called()
            self.configure()
            self.evidence(lead)
            self.evidence(lead, 'whatsapp', 'granted')
            campaign.status, campaign.queue_status = 'Scheduled', 'queued'
            db.session.commit()
            with patch('app.routes.communications._send_to_recipient', return_value=(True, None)) as send:
                process_scheduled_campaigns()
                send.assert_called_once()

    def test_explicit_application_consent_is_linked_to_imported_phone(self):
        from app.services.marketing_consent import apply_consent
        lead = self.lead()
        application = ClientApplication(application_ref='SCREEN-TEST', first_names='Test', surname='Example',
            cell_number=lead.cell_number, lapsed_policy_id=lead.id, agent_id=self.user_ids['admin'], branch='A')
        db.session.add(application)
        db.session.flush()
        apply_consent(application, True, whatsapp_explicit=True)
        db.session.commit()
        row = MarketingScreening.query.filter_by(registry='whatsapp').one()
        self.assertEqual(row.lapsed_policy_id, lead.id)
        self.assertEqual((row.phone_number,row.result), ('27821234567','granted'))
        apply_consent(application, False, whatsapp_explicit=True)
        db.session.commit()
        self.assertFalse(clearance(lead)[0])

    def test_reset_preserves_known_registry_block_as_suppression(self):
        from app.services.data_reset import reset_operational_data
        lead = self.lead()
        self.evidence(lead, result='listed')
        reset_operational_data()
        db.session.commit()
        self.assertEqual(MarketingScreening.query.count(), 0)
        self.assertEqual(LapsedPolicy.query.count(), 0)
        self.assertEqual(ContactSuppression.query.filter_by(source='marketing_screening').count(), 1)

    def test_phone_deletion_removes_screening_numbers_and_preserves_suppression(self):
        from app.services.phone_deletion import delete_phone
        lead = self.lead()
        self.evidence(lead, result='listed')
        delete_phone(lead.cell_number)
        db.session.commit()
        self.assertEqual(MarketingScreening.query.count(), 0)
        self.assertEqual(ContactSuppression.query.count(), 1)

    def test_campaign_filtered_group_retains_screening_selection(self):
        lead, other = self.lead(), self.lead('B')
        self.evidence(lead, result='listed')
        self.evidence(other)
        campaign, _ = self.campaign(lead)
        campaign_id = campaign.id
        self.login('admin')
        response = self.client.post(f'/communications/{campaign_id}/add-filtered-group',
            data={'registry':'dmasa','screening':'clear'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('screening=clear', response.location)
        self.assertEqual({r.lapsed_policy_id for r in CampaignRecipient.query.filter_by(campaign_id=campaign_id)},
                         {lead.id, other.id})
