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

    def test_sent_number_is_removed_even_on_another_policy(self):
        ids,cid=self.seed()
        db.session.add(CampaignRecipient(campaign_id=cid,lapsed_policy_id=ids[0],secure_token='sent',whatsapp_status='Delivered'))
        db.session.commit();self.login('admin')
        response=self.client.get(f'/communications/{cid}?q=DUP-')
        self.assertIn(b'campaign-sent',response.data)
        picker=response.data.split(b'data-client-results>')[1].split(b'</tbody>')[0]
        self.assertNotIn(b'name="policy_ids"',picker)
        self.assertNotIn(b'Add all matching clients',response.data)
        self.assertIn(b'Select next 5 clients',response.data)
        policies=LapsedPolicy.query.filter(LapsedPolicy.id.in_(ids)).order_by(LapsedPolicy.id.desc()).all()
        rows,states=audience_rows(policies,CommunicationCampaign.query.get(cid).recipients)
        self.assertEqual(rows,[])
        self.assertEqual(states,{})

    def test_history_across_campaigns_hides_successes_and_sorts_failures_last(self):
        from app.services.campaign_selection import select_recipients
        ids,cid=self.seed()
        old=CommunicationCampaign(name='Earlier',message_body='Test',created_by_id=self.user_ids['admin'])
        failed=LapsedPolicy(initials='Failed',surname='Client',branch='A',cell_number='0822222222')
        fresh=LapsedPolicy(initials='Fresh',surname='Client',branch='A',cell_number='0823333333')
        db.session.add_all([old,failed,fresh]);db.session.flush()
        db.session.add_all([
            CampaignRecipient(campaign_id=old.id,lapsed_policy_id=ids[0],secure_token='old-success',whatsapp_status='Read'),
            CampaignRecipient(campaign_id=old.id,lapsed_policy_id=failed.id,secure_token='old-failure',whatsapp_status='Failed')])
        db.session.commit()
        policies=[failed,db.session.get(LapsedPolicy,ids[2]),fresh]
        rows,states=audience_rows(policies,[])
        self.assertEqual([p.id for p in rows],[fresh.id,failed.id])
        self.assertTrue(states[failed.id]['selectable'])
        self.assertEqual(states[failed.id]['status'],'Failed')
        # A stale or crafted selection must use the same exclusions as the picker.
        campaign=db.session.get(CommunicationCampaign,cid)
        selected=select_recipients(campaign,policies)
        self.assertEqual({r.lapsed_policy_id for r in CampaignRecipient.query.filter(CampaignRecipient.id.in_(selected))},{fresh.id,failed.id})
        self.login('admin')
        page=self.client.get(f'/communications/{cid}').data
        picker=page.split(b'data-client-results>')[1].split(b'</tbody>')[0]
        self.assertLess(picker.index(b'Fresh Client'),picker.index(b'Failed Client'))
        self.assertIn(b'Failed',picker)
        self.assertIn(b'value="all">Select all clients',page)

    def test_success_in_any_campaign_wins_over_a_later_failure(self):
        ids,cid=self.seed()
        for i,status in enumerate(['Sent','Failed']):
            db.session.add(CampaignRecipient(campaign_id=cid,lapsed_policy_id=ids[i],secure_token='history-'+str(i),whatsapp_status=status))
        db.session.commit()
        rows,_=audience_rows(LapsedPolicy.query.filter(LapsedPolicy.id.in_(ids)).all(),[])
        self.assertEqual(rows,[])

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
