import io
import os
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['ENABLE_WHATSAPP_SCHEDULER'] = '0'
os.environ['AUTO_CREATE_TABLES'] = '1'

from app import create_app, db
from app.models import User, Role, LapsedPolicy, RecoveryCallLog, CallSummary, CallRecording, ComplianceFlag
from app.services.call_recordings import validate_recording, ensure_recording_columns, MAX_RECORDING_BYTES


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b'\0\0' * 80)
    return output.getvalue()


class AnalyticsSecurityTests(unittest.TestCase):
    def setUp(self):
        # Keep these route tests independent of the live licensing service.
        # test_module_license.py separately exercises the entitlement gate.
        license_check = patch('app.module_license.entitlement', return_value={'managed': False, 'active': True})
        license_check.start()
        self.addCleanup(license_check.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.config.update(TESTING=True, UPLOAD_FOLDER=str(Path(self.tmp.name) / 'uploads'))
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        from flask import g
        def clear_cached_user():
            g.pop('_login_user', None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, clear_cached_user)
        roles = {name: Role(name=name) for name in ('Super Admin', 'Branch Manager', 'Agent')}
        self.users = {}
        for name, role, branch in [('admin', 'Super Admin', 'A'), ('manager', 'Branch Manager', 'A'),
                                   ('other', 'Branch Manager', 'B'), ('unassigned', 'Branch Manager', ''),
                                   ('agent', 'Agent', 'A')]:
            user = User(name=name, email=name + '@example.test', role=roles[role], branch=branch,
                        active=True, owner_approved_at=datetime.utcnow(), password_hash='unused')
            db.session.add(user)
            self.users[name] = user
        db.session.flush()
        self.calls = {}
        for branch in ('A', 'B'):
            policy = LapsedPolicy(policy_number=branch, branch=branch)
            db.session.add(policy)
            db.session.flush()
            call = RecoveryCallLog(lapsed_policy_id=policy.id, agent_id=self.users['agent'].id,
                                   outcome='Interested', notes='private-notes-' + branch)
            db.session.add(call)
            db.session.flush()
            self.calls[branch] = call.id
            db.session.add_all([
                CallSummary(call_log_id=call.id, summary='private-summary-' + branch),
                ComplianceFlag(call_log_id=call.id, details='private-flag-' + branch),
                CallRecording(call_log_id=call.id, agent_id=self.users['agent'].id,
                              file_path='database:test', original_filename='private-recording-' + branch + '.wav',
                              file_data=wav_bytes(), content_type='audio/wav'),
            ])
        db.session.commit()
        self.user_ids = {name: user.id for name, user in self.users.items()}
        self.recording_ids = {branch: CallRecording.query.filter_by(call_log_id=call_id).one().id
                              for branch, call_id in self.calls.items()}
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.tmp.cleanup()

    def login(self, name):
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = str(self.user_ids[name])
            session['_fresh'] = True

    def csrf(self):
        self.assertEqual(self.client.get('/analytics/').status_code, 200)
        with self.client.session_transaction() as session:
            return session['analytics_csrf']

    def upload(self, call_id, token, filename='call.wav', data=None):
        return self.client.post(f'/analytics/upload-recording/{call_id}', data={
            'analytics_csrf': token, 'recording': (io.BytesIO(wav_bytes() if data is None else data), filename),
        })

    def test_anonymous_and_agents_cannot_use_analytics_routes(self):
        self.assertEqual(self.client.get('/analytics/').status_code, 302)
        self.assertEqual(self.client.get(f'/analytics/recordings/{self.recording_ids["A"]}/download').status_code, 302)
        self.login('agent')
        for path in ('/analytics/', f'/analytics/recordings/{self.recording_ids["A"]}/download'):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post(f'/analytics/summarize/{self.calls["A"]}').status_code, 403)
        self.assertEqual(self.upload(self.calls['A'], '').status_code, 403)

    def test_dashboard_is_scoped_for_managers_and_global_for_admins(self):
        self.login('manager')
        page = self.client.get('/analytics/').data
        for kind in ('summary', 'flag', 'recording'):
            self.assertIn(('private-' + kind + '-A').encode(), page)
            self.assertNotIn(('private-' + kind + '-B').encode(), page)
        self.login('unassigned')
        self.assertNotIn(b'private-summary-', self.client.get('/analytics/').data)
        self.login('admin')
        page = self.client.get('/analytics/').data
        self.assertIn(b'private-summary-A', page)
        self.assertIn(b'private-summary-B', page)

    def test_cross_branch_writes_and_downloads_are_denied(self):
        self.login('manager')
        token = self.csrf()
        count = CallSummary.query.count()
        self.assertEqual(self.client.post(f'/analytics/summarize/{self.calls["B"]}', data={'analytics_csrf': token}).status_code, 404)
        self.assertEqual(self.upload(self.calls['B'], token).status_code, 404)
        self.assertEqual(self.client.get(f'/analytics/recordings/{self.recording_ids["B"]}/download').status_code, 404)
        self.assertEqual(CallSummary.query.count(), count)
        self.assertEqual(CallRecording.query.count(), 2)
        self.assertEqual(self.client.post(f'/analytics/summarize/{self.calls["A"]}', data={'analytics_csrf': token}).status_code, 302)
        self.assertEqual(CallSummary.query.count(), count + 1)

    def test_upload_is_durable_private_and_does_not_overwrite_same_filename(self):
        self.login('manager')
        token = self.csrf()
        for _ in range(2):
            self.assertEqual(self.upload(self.calls['A'], token).status_code, 302)
        rows = CallRecording.query.filter_by(original_filename='call.wav').all()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].file_path, rows[1].file_path)
        self.assertFalse(Path(self.app.config['UPLOAD_FOLDER']).exists())
        row_ids = [row.id for row in rows]
        for row_id in row_ids:
            row = db.session.get(CallRecording, row_id)
            self.assertEqual(row.file_data, wav_bytes())
            response = self.client.get(f'/analytics/recordings/{row.id}/download')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, wav_bytes())
            self.assertIn('attachment', response.headers['Content-Disposition'])
            self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_invalid_uploads_and_missing_csrf_do_not_create_records(self):
        self.login('manager')
        token = self.csrf()
        self.assertEqual(self.upload(self.calls['A'], '').status_code, 400)
        self.assertEqual(self.client.post(f'/analytics/summarize/{self.calls["A"]}').status_code, 400)
        for filename, data in [('attack.html', b'<html>'), ('fake.wav', b'<html>'), ('empty.wav', b'')]:
            self.assertEqual(self.upload(self.calls['A'], token, filename, data).status_code, 302)
        self.assertEqual(CallRecording.query.count(), 2)
        with self.assertRaises(ValueError):
            validate_recording('huge.wav', b'x' * (MAX_RECORDING_BYTES + 1))

    def test_legacy_recording_is_backfilled_and_outside_paths_are_not_read(self):
        self.login('manager')
        folder = Path(self.app.config['UPLOAD_FOLDER']) / 'call_recordings'
        folder.mkdir(parents=True)
        file = folder / 'old.wav'
        file.write_bytes(wav_bytes())
        row = db.session.get(CallRecording, self.recording_ids['A'])
        row.file_data = None
        row.original_filename = 'old.wav'
        row.file_path = str(file)
        db.session.commit()
        self.assertEqual(self.client.get(f'/analytics/recordings/{row.id}/download').data, wav_bytes())
        row = db.session.get(CallRecording, self.recording_ids['A'])
        self.assertEqual(row.file_data, wav_bytes())
        file.unlink()
        self.assertEqual(self.client.get(f'/analytics/recordings/{row.id}/download').status_code, 200)
        outside = Path(self.tmp.name) / 'outside.wav'
        outside.write_bytes(wav_bytes())
        row = db.session.get(CallRecording, self.recording_ids['A'])
        row.file_data = None
        row.file_path = str(outside)
        db.session.commit()
        self.assertEqual(self.client.get(f'/analytics/recordings/{row.id}/download').status_code, 404)

    def test_static_uploads_are_blocked_while_branding_is_available(self):
        static = Path(self.tmp.name) / 'static'
        uploads = static / 'uploads' / 'call_recordings'
        uploads.mkdir(parents=True)
        (uploads / 'old.wav').write_bytes(wav_bytes())
        (static / 'logo.txt').write_text('branding')
        self.app.static_folder = str(static)
        self.assertEqual(self.client.get('/static/uploads/call_recordings/old.wav').status_code, 404)
        response = self.client.get('/static/logo.txt')
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_reset_requires_session_token_and_confirmation(self):
        self.login('admin')
        with patch('app.services.data_reset.reset_operational_data') as reset:
            for token in ('', 'forged'):
                response = self.client.post('/settings/data-reset', data={
                    'confirmation': 'CLEAR ALL TEST DATA', 'data_reset_csrf': token})
                self.assertEqual(response.status_code, 400)
            reset.assert_not_called()
            page = self.client.get('/settings/data-reset')
            self.assertEqual(page.status_code, 200)
            with self.client.session_transaction() as session:
                token = session['data_reset_csrf']
            self.client.post('/settings/data-reset', data={'data_reset_csrf': token, 'confirmation': 'wrong'})
            reset.assert_not_called()
            reset.return_value = dict.fromkeys(['applications', 'imported_clients', 'whatsapp_messages',
                                               'campaign_recipients', 'suppression_records_kept'], 0)
            response = self.client.post('/settings/data-reset', data={
                'data_reset_csrf': token, 'confirmation': 'CLEAR ALL TEST DATA'})
            self.assertEqual(response.status_code, 302)
            reset.assert_called_once_with()

    def test_login_identifies_email_and_provides_recovery_instructions(self):
        page = self.client.get('/auth/login').data
        self.assertIn(b'Email address', page)
        self.assertNotIn(b'Email or username', page)
        self.assertNotIn(b'href="#forgot-password"', page)
        self.assertIn(b'mailto:wjm@martinsdirect.com', page)

    def test_existing_recording_schema_is_upgraded_idempotently(self):
        from sqlalchemy import text, inspect
        with db.engine.begin() as conn:
            conn.execute(text('DROP TABLE call_recordings'))
            conn.execute(text('CREATE TABLE call_recordings (id INTEGER PRIMARY KEY, file_path VARCHAR(500))'))
            conn.execute(text("INSERT INTO call_recordings (id, file_path) VALUES (1, 'legacy.wav')"))
        ensure_recording_columns()
        ensure_recording_columns()
        self.assertTrue({'file_data', 'content_type'} <= {c['name'] for c in inspect(db.engine).get_columns('call_recordings')})
        with db.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT file_path FROM call_recordings WHERE id=1')).scalar(), 'legacy.wav')

    def test_predeploy_preservation_handles_old_schema_and_is_repeatable(self):
        from sqlalchemy import create_engine, text
        from scripts.preserve_call_recordings import preserve
        engine = create_engine('sqlite:///:memory:')
        folder = Path(self.tmp.name) / 'legacy'
        folder.mkdir()
        file = folder / 'old.wav'
        file.write_bytes(wav_bytes())
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE call_recordings (id INTEGER PRIMARY KEY, file_path VARCHAR(500))'))
            conn.execute(text('INSERT INTO call_recordings VALUES (1, :path)'), {'path': str(file)})
            conn.execute(text('INSERT INTO call_recordings VALUES (2, :path)'), {'path': str(folder / 'missing.wav')})
        self.assertEqual(preserve(engine, [folder]), {'saved': 1, 'unavailable': 1})
        self.assertEqual(preserve(engine, [folder]), {'saved': 0, 'unavailable': 1})
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT file_data FROM call_recordings WHERE id=1')).scalar(), wav_bytes())
        self.assertTrue(file.exists())
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
