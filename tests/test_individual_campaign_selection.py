import unittest

from sqlalchemy import text
from tests import test_analytics_security as fixture
from app import db
from app.models import (CampaignRecipient, CommunicationCampaign, CommunicationEvent,
                        ClientApplication, LapsedPolicy)
from app.services.communication_service import preference_for


class IndividualCampaignSelectionTests(unittest.TestCase):
    login = fixture.AnalyticsSecurityTests.login

    def setUp(self):
        fixture.AnalyticsSecurityTests.setUp(self)
        # Reproduce the production foreign-key constraint in SQLite too.
        db.session.execute(text('PRAGMA foreign_keys=ON'))
        self.leads = LapsedPolicy.query.order_by(LapsedPolicy.id).all()
        for lead in self.leads:
            lead.cell_number = '0821234567'
        campaign = CommunicationCampaign(name='Selection regression', message_body='Test',
            audience_type='individual', created_by_id=self.user_ids['admin'])
        db.session.add(campaign)
        db.session.flush()
        recipient = CampaignRecipient(campaign_id=campaign.id, lapsed_policy_id=self.leads[0].id,
                                      secure_token='original-client-token')
        db.session.add(recipient)
        db.session.flush()
        event = CommunicationEvent(campaign_id=campaign.id, recipient_id=recipient.id,
            lapsed_policy_id=self.leads[0].id, event_type='recipient_added')
        db.session.add(event)
        db.session.commit()
        self.campaign_id, self.recipient_id, self.event_id = campaign.id, recipient.id, event.id
        self.lead_ids = [lead.id for lead in self.leads]
        self.login('admin')

    def tearDown(self):
        db.session.rollback()
        db.session.execute(text('PRAGMA foreign_keys=OFF'))
        fixture.AnalyticsSecurityTests.tearDown(self)

    def select(self, policy_id):
        return self.client.post(f'/communications/{self.campaign_id}/add-recipients',
                                data={'policy_ids': str(policy_id)})

    def test_reselecting_same_client_preserves_token_status_and_history(self):
        original = db.session.get(CampaignRecipient, self.recipient_id)
        original.whatsapp_status = 'Sent'
        db.session.commit()
        for _ in range(2):
            self.assertEqual(self.select(self.lead_ids[0]).status_code, 302)
        recipient = CampaignRecipient.query.one()
        self.assertEqual((recipient.id, recipient.secure_token, recipient.whatsapp_status),
                         (self.recipient_id, 'original-client-token', 'Sent'))
        self.assertEqual(CommunicationEvent.query.count(), 1)
        self.assertEqual(CommunicationEvent.query.one().recipient_id, self.recipient_id)

    def test_replace_unused_draft_preserves_original_policy_event(self):
        self.assertEqual(self.select(self.lead_ids[1]).status_code, 302)
        recipient = CampaignRecipient.query.one()
        self.assertEqual(recipient.lapsed_policy_id, self.lead_ids[1])
        self.assertNotEqual(recipient.secure_token, 'original-client-token')
        event = db.session.get(CommunicationEvent, self.event_id)
        db.session.refresh(event)
        self.assertIsNone(event.recipient_id)
        self.assertEqual(event.lapsed_policy_id, self.lead_ids[0])
        self.assertEqual(CommunicationEvent.query.count(), 2)

    def test_client_with_delivery_history_cannot_be_replaced(self):
        db.session.add(CommunicationEvent(campaign_id=self.campaign_id,
            recipient_id=self.recipient_id, event_type='sent'))
        db.session.commit()
        self.assertEqual(self.select(self.lead_ids[1]).status_code, 302)
        self.assertEqual(CampaignRecipient.query.one().lapsed_policy_id, self.lead_ids[0])
        with self.client.session_transaction() as session:
            self.assertTrue(any('Duplicate the campaign' in message for _, message in session['_flashes']))

    def test_linked_application_prevents_replacement(self):
        db.session.add(ClientApplication(application_ref='SELECTION-APP',
            source_campaign_recipient_id=self.recipient_id))
        db.session.commit()
        self.assertEqual(self.select(self.lead_ids[1]).status_code, 302)
        self.assertEqual(CampaignRecipient.query.one().lapsed_policy_id, self.lead_ids[0])
        self.assertEqual(ClientApplication.query.one().source_campaign_recipient_id, self.recipient_id)

    def test_invalid_suppressed_and_out_of_scope_selections_leave_draft_intact(self):
        self.assertEqual(self.select(999999).status_code, 302)
        pref = preference_for(db.session.get(LapsedPolicy, self.lead_ids[1]))
        pref.opted_out_all = True
        db.session.commit()
        self.assertEqual(self.select(self.lead_ids[1]).status_code, 302)
        preference_for(db.session.get(LapsedPolicy, self.lead_ids[1])).opted_out_all = False
        db.session.get(CommunicationCampaign, self.campaign_id).branch = 'A'
        db.session.commit()
        self.login('manager')
        self.assertEqual(self.select(self.lead_ids[1]).status_code, 302)
        self.assertEqual(CampaignRecipient.query.one().lapsed_policy_id, self.lead_ids[0])
        self.assertEqual(CommunicationEvent.query.one().recipient_id, self.recipient_id)
