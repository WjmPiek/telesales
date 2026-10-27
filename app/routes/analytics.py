
import io
import secrets
from werkzeug.utils import secure_filename
from flask import Blueprint, abort, render_template, request, redirect, url_for, flash, session, send_file
from flask_login import login_required, current_user
from app import db
from app.models import RecoveryCallLog, LapsedPolicy, CallSummary, CallRecording, ComplianceFlag, AuditLog
from app.security import is_admin_user, is_branch_manager_user
from app.services.branch_access import scope_by_branch
from app.services.call_recordings import MAX_RECORDING_BYTES, validate_recording, recording_bytes

analytics_bp = Blueprint('analytics', __name__, url_prefix='/analytics')


def _scoped_calls():
    return scope_by_branch(
        RecoveryCallLog.query.join(LapsedPolicy), LapsedPolicy,
        agent_col=RecoveryCallLog.agent_id, allow_agent_fallback=False,
    )


@analytics_bp.before_request
def protect_analytics():
    if not current_user.is_authenticated:
        return  # Route login_required handlers redirect to login.
    if not (is_admin_user() or is_branch_manager_user()):
        abort(403)
    if request.method == 'POST':
        if request.endpoint == 'analytics.upload_recording' and request.content_length is None:
            abort(411)
        if request.content_length and request.content_length > MAX_RECORDING_BYTES + 1024 * 1024:
            abort(413)
        expected = session.get('analytics_csrf', '')
        supplied = request.form.get('analytics_csrf', '')
        if not expected or not secrets.compare_digest(expected, supplied):
            abort(400, 'Refresh Call Analytics before submitting changes.')

def _simple_ai_summary(call):
    text=(call.notes or '') + ' ' + (call.outcome or '')
    lower=text.lower()
    flags=[]
    if 'complaint' in lower or 'angry' in lower: flags.append('Possible complaint')
    if 'not explained' in lower or 'confused' in lower: flags.append('Product explanation risk')
    if 'bank' in lower and 'confirm' not in lower: flags.append('Check debit-order confirmation')
    next_action='Follow normal callback process'
    if 'no answer' in lower: next_action='Retry call later today or tomorrow'
    elif 'interested' in lower: next_action='Send application/signing link'
    elif 'fica' in lower: next_action='Request outstanding FICA documents'
    summary=f"Outcome: {call.outcome}. Notes captured: {(call.notes or '')[:300]}"
    return summary,next_action,flags

@analytics_bp.route('/')
@login_required
def index():
    session.setdefault('analytics_csrf', secrets.token_urlsafe(32))
    call_ids = _scoped_calls().with_entities(RecoveryCallLog.id)
    summaries=CallSummary.query.filter(CallSummary.call_log_id.in_(call_ids)).order_by(CallSummary.created_at.desc()).limit(20).all()
    flags=ComplianceFlag.query.filter(ComplianceFlag.call_log_id.in_(call_ids)).order_by(ComplianceFlag.created_at.desc()).limit(20).all()
    recordings=CallRecording.query.filter(CallRecording.call_log_id.in_(call_ids)).order_by(CallRecording.uploaded_at.desc()).limit(20).all()
    calls = _scoped_calls().order_by(RecoveryCallLog.id.desc()).limit(20).all()
    return render_template('analytics/index.html', summaries=summaries, flags=flags, recordings=recordings, calls=calls)

@analytics_bp.route('/summarize/<int:call_log_id>', methods=['POST'])
@login_required
def summarize(call_log_id):
    call = _scoped_calls().filter(RecoveryCallLog.id == call_log_id).first_or_404()
    summary,next_action,flags=_simple_ai_summary(call)
    cs=CallSummary(call_log_id=call.id, summary=summary, next_best_action=next_action, risk_flags='; '.join(flags))
    db.session.add(cs)
    for f in flags:
        db.session.add(ComplianceFlag(call_log_id=call.id, flag_type=f, severity='medium', details=summary))
    db.session.add(AuditLog(user_id=current_user.id, action='CALL_SUMMARY_CREATED', entity_type='call_log', entity_id=str(call.id), details='AI-style rule summary generated'))
    db.session.commit(); flash('Call summary generated.','success')
    return redirect(url_for('analytics.index'))

@analytics_bp.route('/upload-recording/<int:call_log_id>', methods=['POST'])
@login_required
def upload_recording(call_log_id):
    call = _scoped_calls().filter(RecoveryCallLog.id == call_log_id).first_or_404()
    f=request.files.get('recording')
    if not f or not f.filename:
        flash('Choose a recording file.','warning'); return redirect(url_for('analytics.index'))
    name = secure_filename(f.filename)[:255]
    data = f.stream.read(MAX_RECORDING_BYTES + 1)
    try:
        content_type = validate_recording(name, data)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('analytics.index'))
    rec = CallRecording(call_log_id=call.id, lapsed_policy_id=call.lapsed_policy_id,
                        agent_id=current_user.id, file_path='', original_filename=name,
                        file_data=data, content_type=content_type)
    db.session.add(rec)
    db.session.flush()
    rec.file_path = f'database:call-recording/{rec.id}'
    db.session.add(AuditLog(user_id=current_user.id, action='CALL_RECORDING_UPLOADED', entity_type='call_log', entity_id=str(call.id), details=f'Recording #{rec.id} stored privately'))
    db.session.commit(); flash('Recording uploaded.','success')
    return redirect(url_for('analytics.index'))


@analytics_bp.route('/recordings/<int:recording_id>/download')
@login_required
def download_recording(recording_id):
    call_ids = _scoped_calls().with_entities(RecoveryCallLog.id)
    recording = CallRecording.query.filter(
        CallRecording.id == recording_id, CallRecording.call_log_id.in_(call_ids),
    ).first_or_404()
    data = recording_bytes(recording)
    if data is None:
        abort(404)
    response = send_file(io.BytesIO(data), mimetype='application/octet-stream',
                         download_name=recording.original_filename or 'recording', as_attachment=True, max_age=0)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
