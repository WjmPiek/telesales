from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import test_analytics_security as fixtures
from app import db
from app.models import CommunicationCampaign, CampaignRecipient, LapsedPolicy
from app.services.campaign_schedule import parse_schedule, schedule_sast


class CampaignScheduleTests(unittest.TestCase):
    setUp = fixtures.AnalyticsSecurityTests.setUp
    tearDown = fixtures.AnalyticsSecurityTests.tearDown
    login = fixtures.AnalyticsSecurityTests.login

    def seed(self, recipients=True):
        c = CommunicationCampaign(name='Scheduled test', message_body='Test', branch='A',
                                  created_by_id=self.user_ids['admin'], template_status='Approved')
        db.session.add(c); db.session.flush()
        if recipients:
            p = LapsedPolicy.query.filter_by(branch='A').first()
            db.session.add(CampaignRecipient(campaign_id=c.id, lapsed_policy_id=p.id, secure_token='schedule-test'))
        db.session.commit()
        cid=c.id
        self.login('admin')
        return cid

    def test_eight_sast_is_six_utc_and_roundtrips_for_display(self):
        cid = self.seed()
        response = self.client.post(f'/communications/{cid}/schedule', data={'scheduled_at':'2030-10-31T08:00'})
        self.assertEqual(response.status_code, 302)
        c=db.session.get(CommunicationCampaign,cid)
        self.assertEqual(c.scheduled_at, datetime(2030,10,31,6))
        self.assertEqual(c.queue_status, 'queued')
        self.assertEqual(schedule_sast(c.scheduled_at), '31 Oct 2030 08:00')
        self.assertEqual(parse_schedule('2030-10-31T08:00+02:00'), c.scheduled_at)

    def test_empty_past_and_invalid_schedules_do_not_queue(self):
        cid = self.seed(False)
        for value in ['2030-10-31T08:00', '2020-01-01T08:00', 'invalid']:
            self.client.post(f'/communications/{cid}/schedule', data={'scheduled_at':value})
            self.assertEqual(db.session.get(CommunicationCampaign,cid).queue_status, 'idle')

    def test_due_utc_send_runs_once_and_future_send_waits(self):
        from app.services.whatsapp_campaign_engine import process_scheduled_campaigns
        cid = self.seed(); c=db.session.get(CommunicationCampaign,cid); c.status='Scheduled'; c.queue_status='queued'
        c.scheduled_at=datetime.utcnow()+timedelta(hours=1); db.session.commit()
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)), patch('app.routes.communications._send_to_recipient', return_value=(True,'')) as sender:
            process_scheduled_campaigns(); self.assertEqual(sender.call_count,0)
            c.scheduled_at=datetime.utcnow()-timedelta(seconds=1); db.session.commit()
            process_scheduled_campaigns(); self.assertEqual(sender.call_count,1)
            process_scheduled_campaigns(); self.assertEqual(sender.call_count,1)
