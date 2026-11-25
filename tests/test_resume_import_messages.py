import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock
import test_simple_sales_flow as fixtures
from app import db
from app.models import LapsedPolicy, ClientImportRow, CampaignRecipient, CommunicationCampaign
from app.services.campaign_send_lock import campaign_send_lock
from app.services.whatsapp_campaign_engine import process_scheduled_campaigns


class ResumeImportMessagesTests(unittest.TestCase):
    setUp=fixtures.SimpleSalesFlowTests.setUp
    tearDown=fixtures.SimpleSalesFlowTests.tearDown
    login=fixtures.SimpleSalesFlowTests.login
    workbook=staticmethod(fixtures.SimpleSalesFlowTests.workbook)
    seed=fixtures.SimpleSalesFlowTests.seed
    upload=fixtures.SimpleSalesFlowTests.upload
    create_campaign=fixtures.SimpleSalesFlowTests.create_campaign

    def interrupted_batch(self):
        self.seed();bid,_=self.upload();cid,_=self.create_campaign(bid)
        for i,status in enumerate(['Sent','Failed','Held','Sending','Not Sent']):
            p=LapsedPolicy(initials='Resume',surname=str(i),branch='Brokers',company_id=self.company_ids[0],
                cell_number=f'082123450{i}',recovery_status='Imported')
            db.session.add(p);db.session.flush()
            db.session.add(ClientImportRow(batch_id=bid,policy_id=p.id))
            db.session.add(CampaignRecipient(campaign_id=cid,lapsed_policy_id=p.id,secure_token=f'resume-{i}',whatsapp_status=status))
        db.session.flush()
        campaign=db.session.get(CommunicationCampaign,cid)
        campaign.send_recipient_ids_json=json.dumps([r.id for r in campaign.recipients])
        campaign.queue_status='processing';campaign.status='Scheduled';campaign.scheduled_at=datetime.utcnow()
        db.session.commit()
        return bid,cid

    def test_reopen_saved_file_shows_interruption_and_counts(self):
        bid,cid=self.interrupted_batch()
        self.assertIn(b'View progress / resume unsent messages',self.client.get(f'/sales/imports/{bid}').data)
        page=self.client.get(f'/sales/campaign/{cid}/status')
        self.assertIn(b'Interrupted',page.data);self.assertIn(b'Resume unsent messages',page.data)
        progress=self.client.get(f'/sales/campaign/{cid}/progress').json
        self.assertEqual([progress[k] for k in ('sent','failed','held','review','unsent','total')],[1,1,1,1,1,5])
        self.assertTrue(progress['interrupted'])

    def test_queued_campaign_opens_lightweight_status_instead_of_selection(self):
        _, campaign_id = self.interrupted_batch()
        path = f'/sales/campaign/{campaign_id}'
        with patch('app.services.campaign_audience.audience_rows') as audience, \
             patch('app.routes.communications._refresh_template_status') as refresh_template:
            self.assertEqual(self.client.get(f'{path}/select').location, f'{path}/status')
            self.assertEqual(self.client.post(f'{path}/select',
                data={'sales_csrf': self.token, 'action': 'send'}).location, f'{path}/status')
            audience.assert_not_called()
            refresh_template.assert_not_called()
        self.assertIn(b'Resume unsent messages', self.client.get(f'{path}/status').data)

    def test_progress_aggregates_recipient_statuses_without_loading_recipient_objects(self):
        from sqlalchemy.orm import raiseload
        from app.routes.sales import send_progress

        _, campaign_id = self.interrupted_batch()
        db.session.expire_all()
        campaign = CommunicationCampaign.query.options(raiseload(CommunicationCampaign.recipients)).filter_by(
            id=campaign_id).populate_existing().one()
        progress = send_progress(campaign)
        self.assertEqual([progress[key] for key in ('sent', 'failed', 'held', 'review', 'unsent', 'total')],
                         [1, 1, 1, 1, 1, 5])
        self.assertTrue(progress['interrupted'])

    def test_resume_only_unsent_and_no_duplicate_sender(self):
        bid,cid=self.interrupted_batch()
        url=f'/sales/campaign/{cid}/resume'
        with campaign_send_lock(cid) as acquired:
            self.assertTrue(acquired)
            page=self.client.get(f'/sales/campaign/{cid}/status')
            self.assertNotIn(b'<button class="btn btn-success">Resume',page.data)
            self.client.post(url,data={'sales_csrf':self.token})
            self.assertEqual(db.session.get(CommunicationCampaign,cid).queue_status,'processing')
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)):
            response=self.client.post(url,data={'sales_csrf':self.token})
        self.assertEqual(response.status_code,302)
        campaign=db.session.get(CommunicationCampaign,cid)
        unsent=CampaignRecipient.query.filter_by(campaign_id=cid,whatsapp_status='Not Sent').one()
        self.assertEqual(json.loads(campaign.send_recipient_ids_json),[unsent.id])
        called=[]
        def send(c,r,channel, **kwargs):
            called.append(r.id);r.whatsapp_status='Delivered';return True,None
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)), \
             patch('app.routes.communications._send_to_recipient',side_effect=send):
            process_scheduled_campaigns();process_scheduled_campaigns()
        self.assertEqual(called,[unsent.id])
        progress=self.client.get(f'/sales/campaign/{cid}/progress').json
        self.assertEqual((progress['total'],progress['sent'],progress['unsent']),(5,2,0))

    def test_crash_mid_attempt_is_durable_and_does_not_retry_blindly(self):
        _,cid=self.interrupted_batch()
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)):
            self.client.post(f'/sales/campaign/{cid}/resume',data={'sales_csrf':self.token})
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)), \
             patch('app.routes.communications._send_to_recipient',side_effect=SystemExit('simulated crash')):
            with self.assertRaises(SystemExit):process_scheduled_campaigns()
        self.assertEqual(CampaignRecipient.query.filter_by(campaign_id=cid,whatsapp_status='Sending').count(),2)
        with patch('app.routes.communications._send_to_recipient') as sender:
            process_scheduled_campaigns();sender.assert_not_called()
        progress = self.client.get(f'/sales/campaign/{cid}/progress').json
        self.assertEqual(progress['queue_status'], 'held')
        self.assertEqual(progress['review'], 2)

    def test_legacy_interruption_does_not_start_sending_without_manual_resume(self):
        _, campaign_id = self.interrupted_batch()
        with patch('app.routes.communications._send_to_recipient') as provider:
            self.assertEqual(process_scheduled_campaigns()['processed'], 0)
            provider.assert_not_called()
        self.assertTrue(self.client.get(f'/sales/campaign/{campaign_id}/progress').json['interrupted'])

    def test_resume_requires_csrf_and_batch_access(self):
        _,cid=self.interrupted_batch()
        self.assertEqual(self.client.post(f'/sales/campaign/{cid}/resume').status_code,400)
        self.login('manager')
        self.assertEqual(self.client.get(f'/sales/campaign/{cid}/status').status_code,403)

    def test_postgres_sender_lock_releases_even_when_sender_crashes(self):
        connection=MagicMock()
        connection.execute.return_value.scalar.return_value=True
        engine=SimpleNamespace(dialect=SimpleNamespace(name='postgresql'),connect=MagicMock())
        engine.connect.return_value.__enter__.return_value=connection
        with patch('app.services.campaign_send_lock.db',SimpleNamespace(engine=engine)):
            with self.assertRaisesRegex(RuntimeError,'crash'):
                with campaign_send_lock(39) as acquired:
                    self.assertTrue(acquired)
                    raise RuntimeError('crash')
        calls=connection.execute.call_args_list
        self.assertIn('pg_try_advisory_lock',str(calls[0].args[0]))
        self.assertIn('pg_advisory_unlock',str(calls[-1].args[0]))
        self.assertEqual(connection.commit.call_count,2)
