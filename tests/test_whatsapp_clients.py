import unittest
import test_analytics_security as fixtures
from app import db
from app.models import CommunicationCampaign, CampaignRecipient, LapsedPolicy
from app.services.communication_service import record_opt_out


class WhatsAppClientTests(unittest.TestCase):
    setUp = fixtures.AnalyticsSecurityTests.setUp
    tearDown = fixtures.AnalyticsSecurityTests.tearDown
    login = fixtures.AnalyticsSecurityTests.login

    def seed(self):
        policies = [LapsedPolicy(initials='Alice',surname='Delivered',cell_number='0821234567',branch='A'),
                    LapsedPolicy(initials='Alice',surname='Duplicate',cell_number='+27821234567',branch='A'),
                    LapsedPolicy(initials='Bob',surname='Callback',cell_number='0831234567',branch='A'),
                    LapsedPolicy(initials='Private',surname='Otherbranch',cell_number='0841234567',branch='B')]
        campaigns=[CommunicationCampaign(name='Branch '+b,message_body='Test',branch=b,created_by_id=self.user_ids['admin']) for b in ['A','B']]
        db.session.add_all(policies+campaigns);db.session.flush()
        recipients=[]
        for i,p in enumerate(policies):
            r=CampaignRecipient(campaign_id=campaigns[int(i==3)].id,lapsed_policy_id=p.id,secure_token='clients-'+str(i),whatsapp_status='Delivered' if i<2 else 'Sent',response_type='callback' if i==2 else None)
            db.session.add(r);recipients.append(r)
        db.session.flush();record_opt_out(recipients[0],'whatsapp');db.session.commit()

    def test_deduplicated_history_and_combined_filters(self):
        self.seed();self.login('admin')
        response=self.client.get('/communications/clients?q=Alice')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'1 matching client(s)', response.data)
        self.assertIn(b'2 campaign(s)', response.data)
        self.assertIn(b'Opted out</span>', response.data)
        response=self.client.get('/communications/clients?status=callback&branch=A&q=Bob')
        self.assertIn(b'Bob Callback', response.data)
        self.assertNotIn(b'Alice Duplicate', response.data)
        self.assertIn(b'Call me back</a>', response.data)
        self.assertNotIn(b'Bob Callback',self.client.get('/communications/clients?status=opted_out').data)

    def test_branch_scope_and_authentication(self):
        self.seed()
        self.assertEqual(self.client.get('/communications/clients').status_code,302)
        self.login('manager')
        page=self.client.get('/communications/clients').data
        self.assertNotIn(b'Private Otherbranch',page)
        self.assertIn(b'Bob Callback',page)
        self.assertNotIn(b'Private Otherbranch',self.client.get('/communications/clients?branch=B').data)
        self.login('unassigned')
        self.assertNotIn(b'Bob Callback',self.client.get('/communications/clients').data)
