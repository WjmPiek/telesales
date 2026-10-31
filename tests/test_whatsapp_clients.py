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

    def test_unsent_drafts_are_hidden_and_failed_attempts_can_be_retried(self):
        self.seed()
        campaign=CommunicationCampaign.query.filter_by(branch='A').first()
        for name,status,phone in [('Onlydraft','Not Sent','0851234567'),
                                  ('Failedattempt','Failed','0861234567'),
                                  ('Heldattempt','Held','0871234567'),
                                  ('Neverselected',None,'0881234567')]:
            p=LapsedPolicy(initials=name,surname='Example',branch='A',cell_number=phone)
            db.session.add(p);db.session.flush()
            if status:
                db.session.add(CampaignRecipient(campaign_id=campaign.id,lapsed_policy_id=p.id,
                    secure_token=name,whatsapp_status=status))
        # A newer draft for an already contacted phone must not hide its delivery.
        c=CommunicationCampaign(name='Unsent draft',message_body='Test',branch='A',created_by_id=self.user_ids['admin'])
        db.session.add(c);db.session.flush()
        alice=LapsedPolicy.query.filter_by(initials='Alice').first()
        db.session.add(CampaignRecipient(campaign_id=c.id,lapsed_policy_id=alice.id,secure_token='newdraft',whatsapp_status='Not Sent'))
        db.session.commit();self.login('admin')
        page=self.client.get('/communications/clients').data
        self.assertNotIn(b'Onlydraft Example',page)
        self.assertNotIn(b'Neverselected Example',page)
        self.assertNotIn(b'Unsent draft',page.split(b'<tbody>')[-1])
        self.assertIn(b'Failedattempt Example',page)
        self.assertIn(b'Heldattempt Example',page)
        self.assertIn(b'Retry failed message',page)
        self.assertIn(b'Delivered</span>',page)
        failed=self.client.get('/communications/clients?status=failed').data
        self.assertIn(b'Failedattempt Example',failed)
        self.assertNotIn(b'Bob Callback',failed)
        self.assertNotIn(b'Onlydraft Example',failed)
