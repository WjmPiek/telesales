import unittest

from sqlalchemy.dialects import postgresql
from tests import test_analytics_security as fixture
from app import db
from app.models import LapsedPolicy, CommunicationCampaign
from app.services.client_directory import franchise_options_query


class ClientDirectoryPostgresTests(unittest.TestCase):
    setUp = fixture.AnalyticsSecurityTests.setUp
    tearDown = fixture.AnalyticsSecurityTests.tearDown
    login = fixture.AnalyticsSecurityTests.login

    def test_postgres_distinct_sorts_by_selected_label_with_bound_parameters(self):
        query = franchise_options_query(LapsedPolicy.query.order_by(LapsedPolicy.id.desc()))
        compiled = query.statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertIn('SELECT DISTINCT', sql)
        self.assertIn('AS franchise_name', sql)
        self.assertEqual(sql.rsplit('ORDER BY ', 1)[1], 'franchise_name')
        self.assertNotIn('ORDER BY coalesce', sql)
        self.assertNotIn('lapsed_policies.id DESC', sql)
        self.assertTrue(compiled.params)  # Exercise the real parameterised form.

    def test_franchises_remain_distinct_sorted_and_use_company_branch_fallbacks(self):
        db.session.add_all([
            LapsedPolicy(franchise='Northcliff',branch='A'),
            LapsedPolicy(franchise='Northcliff',branch='A'),
            LapsedPolicy(franchise='',company_name='Company fallback',branch='A'),
            LapsedPolicy(franchise='',company_name='',branch='Branch fallback'),
            LapsedPolicy(franchise='',company_name='',branch=''),
        ])
        db.session.commit()
        names = [row[0] for row in franchise_options_query(LapsedPolicy.query)]
        self.assertEqual(names,['A','B','Branch fallback','Company fallback','Northcliff'])

    def test_affected_client_import_and_campaign_screens_render(self):
        self.login('admin')
        campaign = CommunicationCampaign(name='Query regression',message_body='No sending',
                                         created_by_id=self.user_ids['admin'])
        db.session.add(campaign)
        db.session.commit()
        campaign_id = campaign.id
        for path in ['/client-files/','/recovery/',f'/communications/{campaign_id}']:
            self.assertEqual(self.client.get(path).status_code,200,path)

    def test_branch_manager_only_sees_own_franchise_options(self):
        self.login('manager')
        response = self.client.get('/client-files/')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'<option value="A"',response.data)
        self.assertNotIn(b'<option value="B"',response.data)
