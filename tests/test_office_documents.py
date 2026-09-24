import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ENABLE_WHATSAPP_SCHEDULER"] = "0"
os.environ["AUTO_CREATE_TABLES"] = "1"

from app import create_app, db
from app.models import ApplicationJourney, AuditLog, ClientApplication, Role, User
from app.services.client_storage import application_folder


class OfficeDocumentTests(unittest.TestCase):
    def setUp(self):
        self.uploads = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.config.update(TESTING=True, UPLOAD_FOLDER=self.uploads.name)
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        role = Role(name="Agent")
        user = User(name="Fictional Agent", email="agent@example.test", role=role,
                    password_hash="unused", branch="A", active=True)
        db.session.add(user)
        db.session.flush()
        application = ClientApplication(
            application_ref="OFFICE-TEST", policy_number="TEST-POLICY",
            first_names="Fictional", surname="Client", id_number="8001015009087",
            email="client@example.test", status="Active", branch="A", agent_id=user.id,
            signed_at=datetime.utcnow())
        db.session.add(application)
        db.session.flush()
        db.session.add(ApplicationJourney(application_id=application.id, activated_at=datetime.utcnow(),
                                          notice_status="Sent", notice_sent_at=datetime.utcnow()))
        self.user_id = user.id
        self.application_id = application.id
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True

    def tearDown(self):
        db.session.remove()
        self.context.pop()
        self.uploads.cleanup()

    def _save_pack(self, *, missing=None):
        application = db.session.get(ClientApplication, self.application_id)
        folder = Path(application_folder(application))
        for name in ["signed_application", "welcome_pack", "popia_consent",
                     "policy_disclosure", "annexure_j1"]:
            if name != missing:
                (folder / f"{name}_{self.application_id}.pdf").write_bytes(b"%PDF-1.4\nsynthetic test file")

    def test_office_send_requires_final_client_email_and_complete_pack(self):
        self._save_pack(missing="annexure_j1")
        with patch("app.routes.applications.send_email") as mail:
            response = self.client.post(f"/applications/{self.application_id}/send-office-documents",
                                        data={"office_email": "office@example.com"}, follow_redirects=True)
            self.assertIn(b"complete signed document pack is not available", response.data)
            mail.assert_not_called()
        application = db.session.get(ClientApplication, self.application_id)
        application.whatsapp_journey.notice_status = "Failed"
        db.session.commit()
        self._save_pack()
        with patch("app.routes.applications.send_email") as mail:
            response = self.client.post(f"/applications/{self.application_id}/send-office-documents",
                                        data={"office_email": "office@example.com"}, follow_redirects=True)
            self.assertIn(b"Send the final approved policy email", response.data)
            mail.assert_not_called()

    def test_office_send_validates_address_logs_delivery_and_can_resend(self):
        self._save_pack()
        with patch("app.routes.applications.send_email", return_value=True) as mail:
            bad = self.client.post(f"/applications/{self.application_id}/send-office-documents",
                                   data={"office_email": "not-an-email"}, follow_redirects=True)
            self.assertIn(b"valid office email address", bad.data)
            mail.assert_not_called()
            good = self.client.post(f"/applications/{self.application_id}/send-office-documents",
                                    data={"office_email": "office@example.com"}, follow_redirects=True)
            self.assertIn(b"Signed document pack sent to the office", good.data)
            self.assertIn(b"Last sent:", good.data)
            self.assertEqual(mail.call_args.args[0], "office@example.com")
            self.assertEqual(len(mail.call_args.args[3]), 5)
            self.assertIn("annexure_j1", " ".join(mail.call_args.args[3]))
            self.assertEqual(AuditLog.query.filter_by(action="Office signed pack sent").count(), 1)
            self.client.post(f"/applications/{self.application_id}/send-office-documents",
                             data={"office_email": "office@example.com"})
            self.assertEqual(mail.call_count, 2)

    def test_office_prompt_is_available_only_after_client_delivery(self):
        page = self.client.get(f"/applications/{self.application_id}?office_prompt=1")
        self.assertIn(b"Send documents to the office", page.data)
        self.assertIn(b"new bootstrap.Modal", page.data)
        application = db.session.get(ClientApplication, self.application_id)
        application.whatsapp_journey.notice_status = "Failed"
        db.session.commit()
        page = self.client.get(f"/applications/{self.application_id}?office_prompt=1")
        self.assertNotIn(b"officeDocumentsModal", page.data)

    def test_failed_office_email_does_not_claim_success(self):
        self._save_pack()
        with patch("app.routes.applications.send_email", return_value=False):
            response = self.client.post(f"/applications/{self.application_id}/send-office-documents",
                                        data={"office_email": "office@example.com"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AuditLog.query.filter_by(action="Office signed pack sent").count(), 0)
        self.assertEqual(AuditLog.query.filter_by(action="Office signed pack failed").count(), 1)


if __name__ == "__main__":
    unittest.main()
