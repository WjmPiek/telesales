import unittest
from datetime import datetime
from unittest.mock import patch

from tests import test_marketing_screening as fixture
from app import db
from app.models import (LapsedPolicy, ClientApplication, CampaignRecipient, CommunicationEvent,
                        ContactSuppression, PolicyProduct)
from app.services.communication_service import preference_for, is_suppressed
from app.services.client_directory import filter_policy_query
from app.services.marketing_screening import clearance


class ClientChoiceDatabaseTests(unittest.TestCase):
    setUp = fixture.MarketingScreeningTests.setUp
    tearDown = fixture.MarketingScreeningTests.tearDown
    login = fixture.MarketingScreeningTests.login
    lead = fixture.MarketingScreeningTests.lead
    campaign = fixture.MarketingScreeningTests.campaign
    evidence = fixture.MarketingScreeningTests.evidence
    configure = fixture.MarketingScreeningTests.configure

    def choice(self):
        lead = self.lead()
        campaign, recipient = self.campaign(lead)
        return lead.id, campaign.id, recipient.id, f'/communications/client/{recipient.secure_token}'

    def opt_out(self, path):
        self.assertEqual(self.client.get(path).status_code, 200)
        with self.client.session_transaction() as session:
            token = session['client_choice_csrf']
        return self.client.post(path, data={'client_choice_csrf':token,'action':'opt-out'})

    def test_opening_choice_link_is_private_and_does_not_change_database(self):
        policy_id, _, _, path = self.choice()
        before = CommunicationEvent.query.count()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Start or continue application', response.data)
        self.assertIn(b'Stop marketing messages', response.data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(CommunicationEvent.query.count(), before)
        self.assertEqual(ContactSuppression.query.count(), 0)
        self.assertEqual(ClientApplication.query.count(), 0)

    def test_opt_out_requires_deliberate_post_and_is_idempotent(self):
        policy_id, _, recipient_id, path = self.choice()
        self.assertEqual(self.client.post(path, data={'action':'opt-out'}).status_code, 400)
        self.assertEqual(ContactSuppression.query.count(), 0)
        self.assertEqual(self.opt_out(path).status_code, 302)
        self.assertEqual(self.opt_out(path).status_code, 302)
        self.assertEqual(ContactSuppression.query.count(), 1)
        self.assertEqual(CommunicationEvent.query.filter_by(event_type='opt_out').count(), 1)
        self.assertEqual(db.session.get(CampaignRecipient,recipient_id).response_type,'opt_out')
        lead = db.session.get(LapsedPolicy,policy_id)
        self.assertTrue(preference_for(lead).opted_out_all)
        self.assertIn(b'Marketing messages have been stopped', self.client.get(path).data)

    def test_opt_out_blocks_duplicate_number_in_another_franchise(self):
        policy_id, _, _, path = self.choice()
        other = self.lead('B')
        other.cell_number = '+27 82 123 4567'
        db.session.commit()
        other_id = other.id
        self.opt_out(path)
        self.assertTrue(is_suppressed(db.session.get(LapsedPolicy,other_id)))
        with self.app.test_request_context():
            rows = filter_policy_query(LapsedPolicy.query, {'contact_permission':'opted_out'}).all()
            self.assertEqual({p.id for p in rows}, {policy_id,other_id})

    def test_opt_out_covers_current_phone_when_email_was_already_suppressed(self):
        from app.services.communication_service import contact_hash
        policy_id, _, _, path = self.choice()
        lead = db.session.get(LapsedPolicy,policy_id)
        lead.email_address = 'synthetic@example.test'
        db.session.add(ContactSuppression(email_hash=contact_hash(lead.email_address),source='earlier_opt_out'))
        db.session.commit()
        self.assertEqual(self.opt_out(path).status_code,302)
        self.assertIsNotNone(ContactSuppression.query.filter_by(phone_hash=contact_hash('27821234567')).first())

    def test_opted_out_client_can_apply_and_application_keeps_import_link(self):
        policy_id, _, recipient_id, path = self.choice()
        lead = db.session.get(LapsedPolicy,policy_id)
        lead.address = 'Synthetic imported address'
        lead.receipt_number = 'SYNTHETIC-RECEIPT'
        product = PolicyProduct(product_name='Single test cover',plan_name='Single',monthly_premium=100,
            cover_amount=10000, min_age=18,max_age=70,active=True)
        db.session.add(product)
        db.session.commit()
        product_id = product.id
        self.opt_out(path)
        self.assertEqual(self.client.get('/join/synthetic-token').status_code,200)
        response = self.client.post('/join/synthetic-token',data={
            'id_number':'8001015009087','total_members':'1','cover_amount':'10000'})
        self.assertEqual(response.status_code,302)
        response = self.client.get(f'/join/synthetic-token/application/{product_id}')
        self.assertEqual(response.status_code,302)
        self.assertIn('/online-application/',response.location)
        application = ClientApplication.query.filter_by(source_campaign_recipient_id=recipient_id).one()
        self.assertEqual(application.lapsed_policy_id,policy_id)
        self.assertEqual(application.residential_address,'Synthetic imported address')
        self.assertEqual(LapsedPolicy.query.count(),2)
        self.assertTrue(is_suppressed(db.session.get(LapsedPolicy,policy_id)))
        self.assertEqual(db.session.get(LapsedPolicy,policy_id).receipt_number,'SYNTHETIC-RECEIPT')
        self.assertEqual(self.client.get('/join/synthetic-token').status_code,302)
        self.assertEqual(ClientApplication.query.count(),1)

    def test_archived_application_is_unavailable_but_opt_out_still_works(self):
        _, campaign_id, _, path = self.choice()
        from app.models import CommunicationCampaign
        db.session.get(CommunicationCampaign,campaign_id).status='Archived'
        db.session.commit()
        response = self.client.get(path)
        self.assertIn(b'no longer available',response.data)
        self.assertNotIn(b'>Start or continue application</a>',response.data)
        self.assertEqual(self.client.get('/join/synthetic-token').status_code,404)
        self.assertEqual(self.opt_out(path).status_code,302)

    def test_contact_permission_filters_combine_with_franchise_and_dates(self):
        lead, other = self.lead(), self.lead('B')
        self.evidence(lead,'whatsapp','granted')
        self.login('admin')
        with self.app.test_request_context():
            self.assertEqual([p.id for p in filter_policy_query(LapsedPolicy.query, {
                'contact_permission':'consented','franchise':'Northcliff','year':'2023','month':'6'})],[lead.id])
            self.assertEqual([p.id for p in filter_policy_query(LapsedPolicy.query, {
                'contact_permission':'unknown'})],[other.id])
        self.assertEqual(self.client.get('/client-files/?contact_permission=bad').status_code,400)
        self.assertEqual(self.client.get('/client-files/?contact_permission=consented').status_code,200)

    def test_internal_mode_keeps_consent_and_known_suppression_protection(self):
        lead = self.lead()
        self.assertFalse(clearance(lead)[0])
        self.evidence(lead,'whatsapp','granted')
        self.assertTrue(clearance(lead)[0])
        self.evidence(lead,'dmasa','listed')
        self.assertFalse(clearance(lead)[0])

    def test_configured_external_requirements_are_not_silently_removed(self):
        lead = self.lead()
        self.evidence(lead,'whatsapp','granted')
        self.configure(['dmasa'])
        self.assertFalse(clearance(lead)[0])
        self.evidence(lead)
        self.assertTrue(clearance(lead)[0])

    def test_staff_can_copy_choice_link_from_campaign_and_client_file(self):
        policy_id, campaign_id, _, path = self.choice()
        self.login('admin')
        response = self.client.get(f'/communications/{campaign_id}')
        self.assertEqual(response.status_code,200)
        self.assertIn(path.encode(),response.data)
        response = self.client.get(f'/client-files/?policy_id={policy_id}')
        self.assertEqual(response.status_code,200)
        self.assertIn(path.encode(),response.data)
        self.login('other')
        self.assertEqual(self.client.get(f'/client-files/?policy_id={policy_id}').status_code,403)

    def test_unknown_link_does_not_expose_clients_or_send_messages(self):
        self.choice()
        with patch('app.routes.communications.send_whatsapp_template_image') as send:
            self.assertEqual(self.client.get('/communications/client/unknown-token').status_code,404)
            self.assertEqual(self.client.get('/communications/client/synthetic-token').status_code,200)
            send.assert_not_called()
