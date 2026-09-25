import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import user_delete_protection as protection


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "FROM pg_trigger" in sql:
            return SimpleNamespace(all=lambda: self.rows)
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class UserDeleteProtectionTests(unittest.TestCase):
    def _db(self, delete_definition, truncate_definition):
        rows = [
            SimpleNamespace(tgname="protect_configuration_delete", definition=delete_definition),
            SimpleNamespace(tgname="protect_configuration_truncate", definition=truncate_definition),
        ]
        session = FakeSession(rows)
        return SimpleNamespace(engine=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
                               session=session)

    def test_replaces_only_known_users_delete_guard(self):
        fake_db = self._db(protection._LEGACY_DELETE, protection._PROTECTED_TRUNCATE)
        with patch.object(protection, "db", fake_db):
            self.assertTrue(protection.ensure_scoped_user_delete_guard())
        self.assertEqual(fake_db.session.commits, 1)
        self.assertTrue(any("OLD.id" in sql for sql in fake_db.session.statements))
        self.assertTrue(any("FOR EACH ROW" in sql for sql in fake_db.session.statements))
        self.assertTrue(any("DROP TRIGGER protect_configuration_delete ON public.users" in sql
                            for sql in fake_db.session.statements))
        self.assertFalse(any("DROP TRIGGER protect_configuration_truncate" in sql
                             for sql in fake_db.session.statements))

    def test_does_not_touch_already_scoped_guard(self):
        fake_db = self._db(protection._SCOPED_DELETE, protection._PROTECTED_TRUNCATE)
        with patch.object(protection, "db", fake_db):
            self.assertFalse(protection.ensure_scoped_user_delete_guard())
        self.assertEqual(fake_db.session.commits, 0)
        self.assertEqual(len(fake_db.session.statements), 1)

    def test_refuses_unknown_configuration(self):
        fake_db = self._db(protection._LEGACY_DELETE, "unexpected")
        with patch.object(protection, "db", fake_db):
            with self.assertRaises(RuntimeError):
                protection.ensure_scoped_user_delete_guard()
        self.assertEqual(fake_db.session.commits, 0)


if __name__ == "__main__":
    unittest.main()
