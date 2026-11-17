import unittest
from sqlalchemy import event
import test_analytics_security as fixtures
from app import db
from app.models import LapsedPolicy, ContactCommunicationPreference, ContactSuppression
from app.services.contact_permissions import allowed_contacts
from app.services.communication_service import contact_hash
from app.services.campaign_audience import audience_rows


class ContactPermissionPerformanceTests(unittest.TestCase):
    setUp = fixtures.AnalyticsSecurityTests.setUp
    tearDown = fixtures.AnalyticsSecurityTests.tearDown
    login = fixtures.AnalyticsSecurityTests.login

    def populate(self, count=500):
        policies = [LapsedPolicy(policy_number=f'PERF-{i}', initials='Test', surname=f'Client {i}',
            branch='A', cell_number=f'082{i:07d}', email_address=f'client{i}@example.test',
            recovery_status='Imported', assigned_agent_id=self.user_ids['agent']) for i in range(count)]
        db.session.add_all(policies); db.session.commit()
        return LapsedPolicy.query.filter(LapsedPolicy.policy_number.like('PERF-%')).all()

    def measure(self, operation):
        statements = []
        def record(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', record)
        try:
            result = operation()
        finally:
            event.remove(db.engine, 'before_cursor_execute', record)
        return result, statements

    def test_500_client_picker_uses_bulk_reads_and_creates_no_preferences(self):
        policies = self.populate()
        (rows, states), statements = self.measure(lambda: audience_rows(policies, []))
        self.assertEqual(len(rows), 500)
        self.assertTrue(all(state['selectable'] for state in states.values()))
        self.assertLessEqual(len(statements), 5)
        self.assertFalse(any(s.lstrip().upper().startswith(('INSERT', 'UPDATE')) for s in statements))
        self.assertEqual(ContactCommunicationPreference.query.count(), 0)

    def test_reminders_and_overview_do_not_query_once_per_client(self):
        self.populate(); self.login('admin')
        for path in ('/recovery/callback-reminders', '/recovery/callbacks'):
            response, statements = self.measure(lambda: self.client.get(path))
            self.assertEqual(response.status_code, 200)
            self.assertLess(len(statements), 60, (path, len(statements)))
            self.assertFalse(any(s.lstrip().upper().startswith('INSERT') for s in statements))
        self.assertEqual(ContactCommunicationPreference.query.count(), 0)

    def test_bulk_checks_keep_channel_opt_out_and_hash_suppressions(self):
        policies = self.populate(5)
        db.session.add_all([
            ContactCommunicationPreference(lapsed_policy_id=policies[0].id, opted_out_all=True),
            ContactCommunicationPreference(lapsed_policy_id=policies[1].id, telephone_allowed=False, whatsapp_allowed=True),
            ContactSuppression(phone_hash=contact_hash('27820000002')),
            ContactSuppression(email_hash=contact_hash('client3@example.test')),
        ])
        db.session.commit()
        phones = allowed_contacts(policies, 'telephone')
        whatsapp = allowed_contacts(policies, 'whatsapp')
        self.assertEqual([phones[p.id] for p in policies], [False, False, False, False, True])
        self.assertEqual([whatsapp[p.id] for p in policies], [False, True, False, False, True])
