import unittest
from datetime import datetime, timedelta
import test_analytics_security as fixtures
from app import db
from app.models import (LapsedPolicy, ClientApplication, ClientImportBatch, ClientImportRow,
                        CompanyGroupState, CommunicationCampaign, CampaignRecipient)


class CallbackOverviewTests(unittest.TestCase):
    setUp = fixtures.AnalyticsSecurityTests.setUp
    tearDown = fixtures.AnalyticsSecurityTests.tearDown
    login = fixtures.AnalyticsSecurityTests.login

    def seed(self):
        companies = [CompanyGroupState(company_name='Northcliff', branch='A', status='Active'),
                     CompanyGroupState(company_name='Brokers', branch='B', status='Active')]
        db.session.add_all(companies); db.session.flush()
        self.batches = []
        for n, (branch, description) in enumerate([('A', 'First Northcliff import'),
                                                   ('A', 'Second Northcliff import'),
                                                   ('B', 'Krugersdorp import')]):
            p = LapsedPolicy(policy_number=f'POL-{n}', initials='Test', surname=f'Client-{n}',
                branch=branch, company_id=companies[branch == 'B'].id, cell_number=f'082123450{n}',
                assigned_agent_id=self.user_ids['agent'], recovery_status='Callback',
                next_action_date=datetime.now().date() - timedelta(days=1))
            batch = ClientImportBatch(filename=f'Report-{n}.xlsx', description=description,
                company_id=p.company_id, created_by_id=self.user_ids['admin'])
            db.session.add_all([p, batch]); db.session.flush()
            db.session.add(ClientImportRow(batch_id=batch.id, policy_id=p.id))
            db.session.add(ClientApplication(application_ref=f'APP-{n}', lapsed_policy_id=p.id,
                first_names='Applicant', surname=f'Person-{n}', branch=branch, agent_id=self.user_ids['agent'],
                status='Signed', signed_at=datetime.utcnow()))
            self.batches.append(batch.id)
        db.session.commit()

    def test_branch_and_saved_import_filter_callbacks_and_submitted_applications(self):
        self.seed(); self.login('admin')
        response = self.client.get('/recovery/callbacks')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('aria-label="A work"', html); self.assertIn('aria-label="B work"', html)
        self.assertIn('Krugersdorp import', html)
        response = self.client.get(f'/recovery/callbacks?branch=A&import=batch:{self.batches[1]}')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('POL-1', html); self.assertIn('APP-1', html)
        self.assertNotIn('POL-0', html); self.assertNotIn('APP-0', html)
        self.assertNotIn('POL-2', html); self.assertNotIn('APP-2', html)

    def test_import_choices_and_tampered_filters_keep_branch_scope(self):
        self.seed(); self.login('manager')
        html = self.client.get('/recovery/callbacks').get_data(as_text=True)
        self.assertIn('First Northcliff import', html)
        self.assertNotIn('Krugersdorp import', html)
        self.assertNotIn('APP-2', html)
        self.assertEqual(self.client.get('/recovery/callbacks?branch=B').status_code, 403)
        self.assertEqual(self.client.get(f'/recovery/callbacks?import=batch:{self.batches[2]}').status_code, 404)
        self.assertEqual(self.client.get('/recovery/callbacks?import=batch:bogus').status_code, 404)

    def test_legacy_imports_remain_filterable(self):
        self.seed(); self.login('admin')
        p = LapsedPolicy(policy_number='LEGACY-ONLY', branch='A', source_filename='Old report.xlsx',
                         recovery_status='Callback', next_action_date=datetime.now().date())
        db.session.add(p); db.session.commit()
        response = self.client.get('/recovery/callbacks', query_string={'branch':'A','import':'file:Old report.xlsx'})
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('LEGACY-ONLY', html); self.assertNotIn('POL-0', html)

    def test_progress_counts_clients_and_existing_send_history(self):
        from flask import template_rendered
        self.seed(); self.login('admin')
        policy = LapsedPolicy.query.join(ClientImportRow).filter(ClientImportRow.batch_id==self.batches[0]).one()
        campaign = CommunicationCampaign(name='Sent campaign', company_id=policy.company_id,
            branch='A', message_body='Test', created_by_id=self.user_ids['admin'])
        db.session.add(campaign); db.session.flush()
        db.session.add(CampaignRecipient(campaign_id=campaign.id, lapsed_policy_id=policy.id,
            secure_token='test-overview-sent', whatsapp_status='Read'))
        db.session.commit()
        captured = []
        def record(sender, template, context, **extra): captured.append(context)
        template_rendered.connect(record, self.app)
        try:
            self.assertEqual(self.client.get('/recovery/callbacks?branch=A').status_code, 200)
        finally:
            template_rendered.disconnect(record, self.app)
        item = next(i for i in captured[-1]['import_summaries'] if i['key']==f'batch:{self.batches[0]}')
        self.assertEqual((item['clients'], item['sent'], item['callbacks'], item['applications']), (1,1,1,1))

    def test_landing_page_does_not_load_non_callback_client_or_history_objects(self):
        from sqlalchemy import event

        self.seed()
        self.login('admin')
        batch = db.session.get(ClientImportBatch, self.batches[0])
        campaign = CommunicationCampaign(name='Synthetic bulk messaging', company_id=batch.company_id,
            branch='A', message_body='Test', created_by_id=self.user_ids['admin'])
        db.session.add(campaign)
        db.session.flush()
        ids = db.session.scalars(db.insert(LapsedPolicy).returning(LapsedPolicy.id), [
            {'initials': 'A', 'surname': 'Example', 'branch': 'A', 'company_id': batch.company_id,
             'cell_number': f'08255{i:05d}', 'recovery_status': 'Imported'}
            for i in range(200)
        ]).all()
        db.session.execute(db.insert(ClientImportRow), [{'batch_id': batch.id, 'policy_id': pid} for pid in ids])
        db.session.execute(db.insert(CampaignRecipient), [
            {'campaign_id': campaign.id, 'lapsed_policy_id': pid, 'secure_token': f'overview-load-{pid}',
             'whatsapp_status': 'Delivered'} for pid in ids
        ])
        db.session.commit()
        db.session.expunge_all()
        loaded_policies, loaded_recipients = [], []

        def policy_loaded(policy, context):
            loaded_policies.append(policy.id)

        def recipient_loaded(recipient, context):
            loaded_recipients.append(recipient.id)

        event.listen(LapsedPolicy, 'load', policy_loaded)
        event.listen(CampaignRecipient, 'load', recipient_loaded)
        try:
            page = self.client.get('/recovery/callbacks?branch=A')
        finally:
            event.remove(LapsedPolicy, 'load', policy_loaded)
            event.remove(CampaignRecipient, 'load', recipient_loaded)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'<td>201</td><td>200</td>', page.data)
        self.assertEqual(len(loaded_policies), 2)
        self.assertEqual(loaded_recipients, [])
