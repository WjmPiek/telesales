import os
from datetime import datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from sqlalchemy import inspect
from app import db
from app.models import LoginAttempt, AuditLog, User, SystemSetting

security_center_bp = Blueprint('security_center', __name__, url_prefix='/security-center')


def _role_name():
    return (current_user.role.name or '').lower().replace('_', ' ') if current_user.is_authenticated and current_user.role else ''


def is_admin():
    return current_user.is_authenticated and _role_name() in ('admin', 'super admin', 'superadmin')


def _get_setting(key, default='0'):
    s = SystemSetting.query.filter_by(category='Security', key=key).first()
    return (s.value if s and s.value is not None else default)


def _set_setting(key, value, description):
    s = SystemSetting.query.filter_by(category='Security', key=key).first()
    if not s:
        s = SystemSetting(category='Security', key=key)
    s.value = value
    s.description = description
    s.active = (value == '1')
    s.updated_by_id = current_user.id
    db.session.add(s)


def _checklist():
    secret = os.getenv('SECRET_KEY', '')
    database_url = os.getenv('DATABASE_URL', '')
    auto_create = os.getenv('AUTO_CREATE_TABLES', '1')
    postgres = database_url.startswith(('postgres://', 'postgresql://'))
    try:
        documents_in_database = postgres and inspect(db.engine).has_table('client_stored_files')
    except Exception:
        documents_in_database = False
    git_deployment = bool(os.getenv('RENDER_GIT_COMMIT'))
    return [
        {
            'key': 'secret_key',
            'label': 'SECRET_KEY set in Render environment',
            'done': bool(secret) and secret not in ('dev', 'change-me', 'secret'),
            'detail': 'Controlled by the Render environment variable SECRET_KEY.',
            'actionable': False,
        },
        {
            'key': 'database_url',
            'label': 'DATABASE_URL uses PostgreSQL',
            'done': postgres,
            'detail': 'Controlled by the Render environment variable DATABASE_URL.',
            'actionable': False,
        },
        {
            'key': 'auto_create_tables_disabled',
            'label': 'AUTO_CREATE_TABLES disabled after migrations are stable',
            'done': auto_create == '0',
            'detail': ('Automatic table creation is disabled. Keep versioned migrations in the deployment process.'
                       if auto_create == '0' else
                       'The app currently creates and updates tables at startup. Keep this enabled until versioned migrations are installed and tested; then set AUTO_CREATE_TABLES=0 in Render.'),
            'actionable': False,
        },
        {
            'key': 'cloud_upload_storage',
            'label': 'Application and FICA files stored in PostgreSQL',
            'done': documents_in_database,
            'detail': 'Signed PDFs, signatures and uploaded identity/FICA documents are saved in client_stored_files. Local files are regenerated from that database copy when needed.',
            'actionable': False,
        },
        {
            'key': 'default_passwords_changed',
            'label': 'Default/admin passwords changed',
            'done': _get_setting('default_passwords_changed') == '1',
            'detail': 'Mark complete after all default or temporary admin passwords have been changed.',
            'actionable': True,
        },
        {
            'key': 'database_backups_configured',
            'label': 'Regular database backups configured',
            'done': _get_setting('database_backups_configured') == '1' or bool(os.getenv('BACKUPS_CONFIGURED') == '1'),
            'detail': 'Mark complete after Render PostgreSQL backups or another backup process is confirmed.',
            'actionable': True,
        },
        {
            'key': 'no_git_folder',
            'label': 'Deployment source verified',
            'done': git_deployment or not os.path.exists(os.path.join(os.getcwd(), '.git')),
            'detail': 'Render is deploying a Git commit.' if git_deployment else 'ZIP deployments should exclude the .git folder.',
            'actionable': False,
        },
    ]


@security_center_bp.route('/')
@login_required
def index():
    if not is_admin():
        return redirect(url_for('main.dashboard'))
    since = datetime.utcnow() - timedelta(days=7)
    attempts = LoginAttempt.query.filter(LoginAttempt.created_at >= since).order_by(LoginAttempt.created_at.desc()).limit(200).all()
    inactive_users = User.query.filter_by(active=False).order_by(User.name).all()
    return render_template('security_center/index.html', attempts=attempts, inactive_users=inactive_users, checklist=_checklist())


@security_center_bp.route('/checklist/<key>/complete', methods=['POST'])
@login_required
def complete_checklist_item(key):
    if not is_admin():
        return redirect(url_for('main.dashboard'))
    allowed = {
        'default_passwords_changed': 'Default/admin passwords were confirmed changed.',
        'database_backups_configured': 'Regular database backups were confirmed configured.',
    }
    if key not in allowed:
        flash('This checklist item is controlled automatically and cannot be changed here.', 'warning')
        return redirect(url_for('security_center.index'))
    _set_setting(key, '1', allowed[key])
    db.session.add(AuditLog(user_id=current_user.id, action='Complete security checklist item', entity_type='Security', entity_id=key, details=allowed[key]))
    db.session.commit()
    flash('Security checklist item marked as complete.', 'success')
    return redirect(url_for('security_center.index'))


@security_center_bp.route('/checklist/<key>/reset', methods=['POST'])
@login_required
def reset_checklist_item(key):
    if not is_admin():
        return redirect(url_for('main.dashboard'))
    allowed = {'default_passwords_changed', 'database_backups_configured'}
    if key not in allowed:
        flash('This checklist item is controlled automatically and cannot be changed here.', 'warning')
        return redirect(url_for('security_center.index'))
    _set_setting(key, '0', 'Security checklist item reset to needs attention.')
    db.session.add(AuditLog(user_id=current_user.id, action='Reset security checklist item', entity_type='Security', entity_id=key, details='Reset to needs attention'))
    db.session.commit()
    flash('Security checklist item reset.', 'info')
    return redirect(url_for('security_center.index'))
