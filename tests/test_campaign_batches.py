import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import test_analytics_security as fixtures
from app import db
from app.models import CommunicationCampaign, CampaignRecipient, LapsedPolicy
from app.services.whatsapp_campaign_engine import process_scheduled_campaigns


class CampaignBatchTests(unittest.TestCase):
    setUp=fixtures.AnalyticsSecurityTests.setUp
    tearDown=fixtures.AnalyticsSecurityTests.tearDown
    login=fixtures.AnalyticsSecurityTests.login

    def seed(self):
        c=CommunicationCampaign(name='Exact batch',message_body='Test',audience_type='group',branch='A',
            template_status='Approved',created_by_id=self.user_ids['admin'])
        policies=[LapsedPolicy(initials='Batch',surname=str(i),branch='A',cell_number='082100'+str(i).zfill(4)) for i in range(20)]
        db.session.add_all([c]+policies);db.session.flush()
        # A previously saved large selection must never expand a new five/ten batch.
        for p in policies:
            db.session.add(CampaignRecipient(campaign_id=c.id,lapsed_policy_id=p.id,secure_token='batch-'+str(p.id)))
        db.session.commit();cid=c.id;ids=[p.id for p in policies]
        self.login('admin')
        return cid,ids

    def queue(self,cid,ids,**extra):
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)):
            return self.client.post(f'/communications/{cid}/queue-selection',data={'policy_ids':list(map(str,ids)),**extra})

    def assert_batch(self,size):
        cid,ids=self.seed()
        self.assertEqual(self.queue(cid,ids[:size]).status_code,302)
        c=db.session.get(CommunicationCampaign,cid)
        self.assertEqual(len(json.loads(c.send_recipient_ids_json)),size)
        called=[]
        def send(c,r,channel):
            called.append(r.lapsed_policy_id);r.whatsapp_status='Delivered';return True,''
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)),patch('app.routes.communications._send_to_recipient',side_effect=send):
            process_scheduled_campaigns();process_scheduled_campaigns()
        self.assertEqual(set(called),set(ids[:size]));self.assertEqual(len(called),size)
        self.assertEqual(CampaignRecipient.query.filter_by(campaign_id=cid,whatsapp_status='Not Sent').count(),20-size)
        page=self.client.get(f'/communications/{cid}').data
        self.assertIn(b'campaign-sent',page)
        # A second batch can reuse the campaign while sent clients are skipped.
        self.queue(cid,ids[:size])
        self.assertEqual(db.session.get(CommunicationCampaign,cid).queue_status,'completed')

    def test_five_is_exact_even_with_twenty_saved_recipients(self): self.assert_batch(5)

    def test_ten_is_exact_even_with_twenty_saved_recipients(self): self.assert_batch(10)

    def test_schedule_freezes_selection_and_sast_without_sending_early(self):
        cid,ids=self.seed()
        self.queue(cid,ids[:5],selection_action='schedule',scheduled_at='2030-10-31T08:00')
        c=db.session.get(CommunicationCampaign,cid)
        self.assertEqual(c.scheduled_at,datetime(2030,10,31,6))
        first=c.send_recipient_ids_json
        self.queue(cid,ids[5:15])
        c=db.session.get(CommunicationCampaign,cid)
        self.assertEqual(c.send_recipient_ids_json,first)
        with patch('app.routes.communications._send_to_recipient') as sender:
            process_scheduled_campaigns();sender.assert_not_called()

    def test_empty_or_foreign_selection_cannot_queue_saved_clients(self):
        cid,ids=self.seed()
        self.queue(cid,[])
        self.assertEqual(db.session.get(CommunicationCampaign,cid).queue_status,'idle')
        self.login('other')
        self.assertEqual(self.queue(cid,ids[:5]).status_code,403)

    def test_duplicate_phone_and_optout_are_not_sent_twice(self):
        from app.services.communication_service import record_opt_out
        cid,ids=self.seed()
        db.session.get(LapsedPolicy,ids[1]).cell_number='+27821000000'
        opted=CampaignRecipient.query.filter_by(campaign_id=cid,lapsed_policy_id=ids[2]).one()
        record_opt_out(opted,'whatsapp');db.session.commit()
        self.queue(cid,ids[:5])
        selected=json.loads(db.session.get(CommunicationCampaign,cid).send_recipient_ids_json)
        self.assertEqual(len(selected),3)
        policy_ids={r.lapsed_policy_id for r in CampaignRecipient.query.filter(CampaignRecipient.id.in_(selected))}
        self.assertNotIn(ids[2],policy_ids)
