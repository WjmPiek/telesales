import unittest
from types import SimpleNamespace
import test_analytics_security as fixtures
from app import db
from app.models import (CompanyGroupState,LapsedPolicy,ClientApplication,CommunicationCampaign,
                        CampaignRecipient,CompanyDocument,HistoricalMemberCover)
from app.services.company_consolidation import consolidate,enabled
from app.services.franchise_routing import destination_company
from app.services.branch_contact import branch_contact


class CompanyConsolidationTests(unittest.TestCase):
    setUp=fixtures.AnalyticsSecurityTests.setUp
    tearDown=fixtures.AnalyticsSecurityTests.tearDown
    login=fixtures.AnalyticsSecurityTests.login

    def seed(self):
        brokers=destination_company();north=destination_company(True)
        extra=CompanyGroupState(company_name='Northcliff',branch='Brokers',status='Active')
        other=CompanyGroupState(company_name='Alberton',branch='Alberton',status='Deleted')
        db.session.add_all([extra,other]);db.session.flush()
        p=LapsedPolicy(initials='North',surname='Client',company_id=extra.id,branch='Brokers',franchise='Northcliff')
        app=ClientApplication(application_ref='MERGE',company_id=extra.id,branch='Brokers',payment_method='Stop Order')
        campaign=CommunicationCampaign(name='Old all companies',branch='Brokers',message_body='Test',created_by_id=self.user_ids['admin'])
        db.session.add_all([p,app,campaign]);db.session.flush()
        db.session.add(CampaignRecipient(campaign_id=campaign.id,lapsed_policy_id=p.id,secure_token='merge-contact',whatsapp_status='Delivered'))
        db.session.add(CompanyDocument(company_id=extra.id,category='bank_confirmation',original_filename='bank.pdf',file_data=b'%PDF-test',file_size=9,checksum_sha256='a'*64,uploaded_by_id=self.user_ids['admin']))
        db.session.add(HistoricalMemberCover(company_id=extra.id,policy_number='OLD',id_number='8001015009087',cover_amount=10000,imported_by_id=self.user_ids['admin']))
        extra.agents.append(self.users['agent']);db.session.commit()
        return brokers.id,north.id,extra.id,p.id,app.id,campaign.id

    def test_preserves_ids_documents_history_and_assignments_with_two_active_companies(self):
        bid,nid,eid,pid,aid,cid=self.seed()
        consolidate(self.user_ids['admin']);db.session.commit()
        self.assertEqual({c.company_name for c in CompanyGroupState.query.filter_by(status='Active')},{"Martin's Brokers","Martin's Northcliff"})
        self.assertEqual(db.session.get(LapsedPolicy,pid).company_id,nid)
        self.assertEqual(db.session.get(LapsedPolicy,pid).franchise,'Northcliff')
        self.assertEqual(db.session.get(ClientApplication,aid).company_id,nid)
        self.assertEqual(db.session.get(CommunicationCampaign,cid).company_id,nid)
        self.assertEqual(db.session.get(CommunicationCampaign,cid).branch,'NORTHCLIFF')
        self.assertEqual(CampaignRecipient.query.filter_by(campaign_id=cid).one().whatsapp_status,'Delivered')
        self.assertEqual(CompanyDocument.query.one().company_id,nid)
        self.assertEqual(HistoricalMemberCover.query.one().company_id,nid)
        self.assertIn(self.user_ids['agent'],[u.id for u in db.session.get(CompanyGroupState,nid).agents])
        self.assertEqual(db.session.get(CompanyGroupState,eid).status,'Deleted')
        self.assertTrue(enabled())
        consolidate(self.user_ids['admin']);db.session.commit()
        self.assertEqual(CompanyGroupState.query.filter_by(status='Active').count(),2)

    def test_old_all_company_recipient_uses_client_branch_and_new_imports_do_not_create_groups(self):
        from app.services.company_groups import get_or_create_company
        bid,nid,*_=self.seed();consolidate(self.user_ids['admin']);db.session.commit()
        self.assertEqual(get_or_create_company('New franchise','Other').id,bid)
        self.assertEqual(get_or_create_company('Northcliff','Brokers').id,nid)
        recipient=SimpleNamespace(policy=SimpleNamespace(branch='NORTHCLIFF'),campaign=SimpleNamespace(branch='Brokers'))
        self.assertEqual(branch_contact(recipient)['phone'],'010 448 0921')
        app=SimpleNamespace(branch='Brokers',policy=SimpleNamespace(branch='NORTHCLIFF'))
        self.assertEqual(branch_contact(app)['phone'],'011 412 4180')

    def test_company_selector_has_only_two_choices_and_cannot_save_all_companies(self):
        self.seed();consolidate(self.user_ids['admin']);db.session.commit();self.login('admin')
        page=self.client.get('/communications/new').data
        self.assertIn(b"Martin&#39;s Northcliff",page)
        self.assertNotIn(b'all companies (admin only)',page)
        self.assertNotIn('Northcliff — Brokers'.encode(),page)
        response=self.client.post('/communications/new',data={'name':'Ambiguous','message_body':'Test'})
        self.assertEqual(response.status_code,302)
        self.assertIsNone(CommunicationCampaign.query.filter_by(name='Ambiguous').first())

    def test_consolidation_route_requires_super_admin_and_csrf(self):
        self.seed();self.login('manager')
        self.assertEqual(self.client.get('/settings/company-consolidation').status_code,403)
        self.login('admin');self.client.get('/settings/company-consolidation')
        self.assertEqual(self.client.post('/settings/company-consolidation').status_code,400)
