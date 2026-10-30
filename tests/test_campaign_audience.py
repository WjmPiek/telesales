import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import test_analytics_security as fixtures
from app import db
from app.models import LapsedPolicy, CommunicationCampaign, CampaignRecipient, WhatsAppMessage
from app.services.campaign_audience import audience_rows


class CampaignAudienceTests(unittest.TestCase):
    setUp=fixtures.AnalyticsSecurityTests.setUp
    tearDown=fixtures.AnalyticsSecurityTests.tearDown
    login=fixtures.AnalyticsSecurityTests.login

    def seed(self):
        rows=[LapsedPolicy(policy_number='DUP-'+str(i),initials='T',surname='Example',cell_number=phone,branch='A')
              for i,phone in enumerate(['0821234567','+27821234567','27821234567'])]
        campaign=CommunicationCampaign(name='Dedup test',message_body='Test',audience_type='group',
            created_by_id=self.user_ids['admin'],image_url='https://example.test/image.png',whatsapp_template_name='test',
            template_status='Approved',send_whatsapp=True)
        db.session.add_all(rows+[campaign]);db.session.commit()
        return [row.id for row in rows],campaign.id

    def test_picker_hides_duplicate_numbers_without_removing_policies(self):
        ids,cid=self.seed()
        policies=LapsedPolicy.query.filter(LapsedPolicy.id.in_(ids)).all()
        rows,states=audience_rows(policies,[])
        self.assertEqual(len(rows),1)
        self.assertTrue(states[rows[0].id]['selectable'])
        self.assertEqual(LapsedPolicy.query.filter(LapsedPolicy.id.in_(ids)).count(),3)

    def test_add_selection_and_old_bulk_endpoint_add_only_one_phone(self):
        ids,cid=self.seed();self.login('admin')
        self.client.post(f'/communications/{cid}/add-recipients',data={'policy_ids':[str(i) for i in ids]})
        self.assertEqual(CampaignRecipient.query.filter_by(campaign_id=cid).count(),1)
        self.client.post(f'/communications/{cid}/add-filtered-group',data={'q':'DUP-'})
        self.assertEqual(CampaignRecipient.query.filter_by(campaign_id=cid).count(),1)

    def test_sent_number_is_green_and_unselectable_even_on_another_policy(self):
        ids,cid=self.seed()
        db.session.add(CampaignRecipient(campaign_id=cid,lapsed_policy_id=ids[0],secure_token='sent',whatsapp_status='Delivered'))
        db.session.commit();self.login('admin')
        response=self.client.get(f'/communications/{cid}?q=DUP-')
        self.assertIn(b'campaign-sent',response.data)
        self.assertIn(b'Sent in this campaign',response.data)
        self.assertNotIn(b'Add all matching clients',response.data)
        self.assertIn(b'Select next 5 clients',response.data)
        policies=LapsedPolicy.query.filter(LapsedPolicy.id.in_(ids)).order_by(LapsedPolicy.id.desc()).all()
        rows,states=audience_rows(policies,CommunicationCampaign.query.get(cid).recipients)
        self.assertTrue(states[rows[0].id]['sent'])
        self.assertFalse(states[rows[0].id]['selectable'])

    def test_sender_and_retry_call_provider_once_for_duplicate_number(self):
        from app.services.whatsapp_campaign_engine import process_scheduled_campaigns
        ids,cid=self.seed();campaign=db.session.get(CommunicationCampaign,cid)
        campaign.status='Scheduled';campaign.queue_status='queued';campaign.scheduled_at=datetime.utcnow()-timedelta(seconds=1)
        for i,policy_id in enumerate(ids):
            db.session.add(CampaignRecipient(campaign_id=cid,lapsed_policy_id=policy_id,secure_token='send-'+str(i)))
        db.session.commit()
        result=SimpleNamespace(ok=True,error=None,message_id='provider-test-id',response_json={})
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)), \
             patch('app.services.marketing_screening.clearance',return_value=(True,'')), \
             patch('app.routes.communications.send_whatsapp_template_image',return_value=result) as provider:
            process_scheduled_campaigns()
            self.assertEqual(provider.call_count,1)
            campaign.status='Scheduled';campaign.queue_status='retry';db.session.commit()
            process_scheduled_campaigns()
            self.assertEqual(provider.call_count,1)
        self.assertEqual(WhatsAppMessage.query.filter_by(provider_message_id='provider-test-id').count(),1)
        self.assertEqual(CampaignRecipient.query.filter_by(campaign_id=cid,whatsapp_status='Skipped').count(),2)
