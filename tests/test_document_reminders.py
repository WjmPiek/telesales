import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ENABLE_WHATSAPP_SCHEDULER"] = "0"
os.environ["AUTO_CREATE_TABLES"] = "1"

from app import create_app, db
from app.models import AuditLog, ClientApplication, SupportingDocumentReminder
from app.services.document_reminders import (cancel_document_reminders,
                                              process_document_reminders,
                                              schedule_document_reminders)


class DocumentReminderTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, BASE_URL="https://example.test")
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.submitted = datetime(2026, 9, 24, 10, 0)
        self.record = ClientApplication(
            application_ref="TEST-REMINDER", first_names="Fictional", surname="Client",
            id_number="8001015009087", email="fictional@example.test", sign_token="synthetic-token",
            signed_at=self.submitted, status="FICA Outstanding")
        db.session.add(self.record)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    @patch("app.routes.signing.send_email", return_value=True)
    @patch("app.routes.signing._fica_status", return_value=(["id_copy"], set(), ["id_copy"], []))
    def test_two_reminders_are_24_hours_apart_and_stop(self, status, mail):
        schedule_document_reminders(self.record)
        self.assertEqual(process_document_reminders(now=self.submitted + timedelta(hours=23))["sent"], 0)
        first = self.submitted + timedelta(hours=24)
        self.assertEqual(process_document_reminders(now=first)["sent"], 1)
        self.assertIn("/sign/synthetic-token/supporting-documents", mail.call_args.args[2])
        self.assertEqual(process_document_reminders(now=first + timedelta(hours=23))["sent"], 0)
        self.assertEqual(process_document_reminders(now=first + timedelta(hours=24))["sent"], 1)
        self.assertEqual(process_document_reminders(now=first + timedelta(hours=48))["sent"], 0)
        self.assertEqual(mail.call_count, 2)
        self.assertEqual(AuditLog.query.filter_by(action="Automated supporting reminder 1").count(), 1)
        self.assertEqual(AuditLog.query.filter_by(action="Automated supporting reminder 2").count(), 1)

    @patch("app.routes.signing.send_email", return_value=True)
    @patch("app.routes.signing._fica_status", return_value=(["id_copy"], {"id_copy"}, [], []))
    def test_received_documents_cancel_before_first_reminder(self, status, mail):
        schedule_document_reminders(self.record)
        stats = process_document_reminders(now=self.submitted + timedelta(hours=24))
        self.assertEqual(stats["cancelled"], 1)
        self.assertIsNotNone(db.session.get(SupportingDocumentReminder, self.record.id).cancelled_at)
        mail.assert_not_called()

    @patch("app.routes.signing.send_email", return_value=False)
    @patch("app.routes.signing._fica_status", return_value=(["id_copy"], set(), ["id_copy"], []))
    def test_failed_delivery_retries_without_advancing_stage(self, status, mail):
        schedule_document_reminders(self.record)
        due = self.submitted + timedelta(hours=24)
        self.assertEqual(process_document_reminders(now=due)["failed"], 1)
        self.assertEqual(process_document_reminders(now=due + timedelta(minutes=30))["failed"], 0)
        self.assertEqual(process_document_reminders(now=due + timedelta(hours=1))["failed"], 1)
        row = db.session.get(SupportingDocumentReminder, self.record.id)
        self.assertIsNone(row.first_sent_at)
        self.assertEqual(mail.call_count, 2)

    @patch("app.routes.signing.send_email", return_value=True)
    @patch("app.routes.signing._fica_status")
    def test_uploads_after_first_reminder_cancel_final_reminder(self, status, mail):
        status.return_value = (["id_copy"], set(), ["id_copy"], [])
        schedule_document_reminders(self.record)
        first = self.submitted + timedelta(hours=24)
        self.assertEqual(process_document_reminders(now=first)["sent"], 1)
        status.return_value = (["id_copy"], {"id_copy"}, [], [])
        self.assertEqual(process_document_reminders(now=first + timedelta(hours=24))["cancelled"], 1)
        self.assertEqual(mail.call_count, 1)

    def test_direct_cancellation_is_idempotent(self):
        schedule_document_reminders(self.record)
        cancel_document_reminders(self.record.id)
        cancel_document_reminders(self.record.id)
        db.session.commit()
        self.assertEqual(AuditLog.query.filter_by(action="Supporting reminders cancelled").count(), 1)


if __name__ == "__main__":
    unittest.main()
