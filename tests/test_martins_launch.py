import os
import unittest

from itsdangerous import URLSafeTimedSerializer

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ENABLE_WHATSAPP_SCHEDULER"] = "0"
os.environ["AUTO_CREATE_TABLES"] = "1"

from app import create_app, db
from app.models import Role, User


class MartinsLaunchTests(unittest.TestCase):
    def setUp(self):
        self.secret = "test-insurance-launch-secret"
        self.previous_secret = os.environ.get("INSURANCE_LAUNCH_SECRET")
        os.environ["INSURANCE_LAUNCH_SECRET"] = self.secret
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        db.session.add_all([Role(name="Admin"), Role(name="Agent")])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        if self.previous_secret is None:
            os.environ.pop("INSURANCE_LAUNCH_SECRET", None)
        else:
            os.environ["INSURANCE_LAUNCH_SECRET"] = self.previous_secret

    def _token(self, **overrides):
        payload = {
            "module": "insurance",
            "email": "owner@example.test",
            "name": "Example Owner",
            "is_admin": False,
            "franchises": ["Example Branch"],
        }
        payload.update(overrides)
        return URLSafeTimedSerializer(
            self.secret, salt="martins-insurance-launch-v1"
        ).dumps(payload)

    def test_valid_launch_provisions_and_signs_in_user(self):
        response = self.client.get(f"/auth/launch?token={self._token()}")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/home"))
        user = User.query.filter_by(email="owner@example.test").one()
        self.assertTrue(user.active)
        self.assertEqual(user.name, "Example Owner")
        self.assertEqual(user.branch, "Example Branch")
        self.assertEqual(user.role.name, "Agent")
        with self.client.session_transaction() as session:
            self.assertEqual(session["_user_id"], str(user.id))

    def test_invalid_or_wrong_module_token_is_rejected(self):
        self.assertEqual(self.client.get("/auth/launch?token=not-valid").status_code, 401)
        wrong = self._token(module="claims")
        self.assertEqual(self.client.get(f"/auth/launch?token={wrong}").status_code, 401)

    def test_protected_admin_launch_gets_admin_role(self):
        token = self._token(
            email="wjm@martinsdirect.com", name="WJM Piek", is_admin=True
        )
        self.assertEqual(self.client.get(f"/auth/launch?token={token}").status_code, 302)
        user = User.query.filter_by(email="wjm@martinsdirect.com").one()
        self.assertEqual(user.role.name, "Admin")


if __name__ == "__main__":
    unittest.main()
