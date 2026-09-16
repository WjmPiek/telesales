from app import db
from flask import Blueprint, render_template, request, send_file, redirect, url_for, flash, abort
import io
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from app.models import (ClientApplication, LapsedPolicy, RecoveryCallLog,
                        TelesalesScriptSession, ClientFicaDocument, WhatsAppContact,
                        WhatsAppConversation, WhatsAppMessage)
from app.security import permission_required
from app.services.branch_access import scope_by_branch, ensure_branch_access
from app.services.whatsapp_service import normalize_phone

client_files_bp = Blueprint('client_files', __name__, url_prefix='/client-files')


def identity_column(column):
    return func.upper(func.replace(func.replace(func.trim(column), ' ', ''), '-', ''))


@client_files_bp.route('/', methods=['GET', 'POST'])
@login_required
@permission_required('recovery.view')
def index():
    identity = ''.join(c for c in request.values.get('id_number', '') if c.isalnum()).upper()
    app_id = request.args.get('application_id', type=int)
    policy_id = request.args.get('policy_id', type=int)
    seed_app = seed_policy = None
    if app_id:
        seed_app = ClientApplication.query.get_or_404(app_id)
        ensure_branch_access(seed_app, agent_attr='agent_id')
        identity = ''.join(c for c in seed_app.id_number or '' if c.isalnum()).upper()
    elif policy_id:
        seed_policy = LapsedPolicy.query.get_or_404(policy_id)
        ensure_branch_access(seed_policy, agent_attr='assigned_agent_id')
        identity = ''.join(c for c in seed_policy.id_number or '' if c.isalnum()).upper()
    applications, policies, calls, scripts, documents, messages = [], [], [], [], [], []
    searched = bool(identity or app_id or policy_id or request.method == 'POST')
    if identity or seed_app or seed_policy:
        pq = scope_by_branch(LapsedPolicy.query, LapsedPolicy, agent_col=LapsedPolicy.assigned_agent_id)
        aq = scope_by_branch(ClientApplication.query, ClientApplication, agent_col=ClientApplication.agent_id)
        policies = pq.filter(identity_column(LapsedPolicy.id_number) == identity).all() if identity else ([seed_policy] if seed_policy else [])
        pids = [p.id for p in policies]
        conditions = [ClientApplication.lapsed_policy_id.in_(pids)]
        if identity:
            conditions.append(identity_column(ClientApplication.id_number) == identity)
        if seed_app:
            conditions.append(ClientApplication.id == seed_app.id)
        applications = aq.filter(or_(*conditions)).order_by(ClientApplication.created_at.desc()).all()
        aids = [a.id for a in applications]
        calls = RecoveryCallLog.query.filter(RecoveryCallLog.lapsed_policy_id.in_(pids)).order_by(RecoveryCallLog.created_at.desc()).all()
        scripts = scope_by_branch(TelesalesScriptSession.query, TelesalesScriptSession, agent_col=TelesalesScriptSession.agent_id).filter(or_(TelesalesScriptSession.lapsed_policy_id.in_(pids), TelesalesScriptSession.application_id.in_(aids))).order_by(TelesalesScriptSession.created_at.desc()).all()
        documents = ClientFicaDocument.query.filter(ClientFicaDocument.application_id.in_(aids)).order_by(ClientFicaDocument.uploaded_at.desc()).all()
        phones = {normalize_phone(p.cell_number) for p in policies} | {normalize_phone(a.cell_number) for a in applications}
        phones.discard('')
        # Apply contact access independently: an accessible application must not expose another branch's chat.
        contacts = scope_by_branch(WhatsAppContact.query, WhatsAppContact, agent_col=WhatsAppContact.assigned_agent_id)
        contacts = contacts.filter(or_(WhatsAppContact.wa_id.in_(phones), WhatsAppContact.phone_number.in_(phones | {'+' + p for p in phones}))).all() if phones else []
        conversations = WhatsAppConversation.query.filter(WhatsAppConversation.contact_id.in_([c.id for c in contacts])).all()
        messages = WhatsAppMessage.query.filter(WhatsAppMessage.conversation_id.in_([c.id for c in conversations])).order_by(WhatsAppMessage.created_at.asc()).all()
    from app.models import ClientStoredFile, ClientCommunication, CommunicationEvent
    from app.services.conversation_history import event
    import json
    timeline=[]
    aids=[a.id for a in applications];pids=[p.id for p in policies]
    for c in calls:
        timeline.append(event(c.created_at,'Call','Call',c.notes or 'No discussion notes recorded.',subject=c.outcome,actor='Employee #'+str(c.agent_id),source='Call log'))
    for s in scripts:
        try:
            answers=json.loads(s.answers_json or '{}')
            body='\n'.join(str(k).replace('_',' ').title()+': '+(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else str(v)) for k,v in answers.items()) if isinstance(answers,dict) else json.dumps(answers,ensure_ascii=False)
        except (ValueError,TypeError):body='See the saved script for the recorded discussion.'
        timeline.append(event(s.completed_at or s.created_at,'Call','Script',body or 'No answers recorded.',subject='Sales script',status=s.status,source='Saved script'))
    for m in messages:
        timeline.append(event(m.created_at,'WhatsApp',m.direction,m.body or '['+m.message_type+' message]',status=m.status,actor=m.sender_user.name if m.sender_user else '',source='WhatsApp'))
    rows=ClientCommunication.query.filter(or_(ClientCommunication.application_id.in_(aids),ClientCommunication.lapsed_policy_id.in_(pids))).all() if aids or pids else []
    for c in rows:
        timeline.append(event(c.occurred_at,c.channel,c.direction,c.body,subject=c.subject,status=c.status,actor=c.actor.name if c.actor else '',source='Employee entry' if c.source=='manual' else 'System',attachments=json.loads(c.attachments_json or '[]')))
    # Older campaign records predate full email logging. Identify them honestly;
    # do not reconstruct a sent message from a campaign that may have been edited.
    for e in CommunicationEvent.query.filter(CommunicationEvent.lapsed_policy_id.in_(pids),CommunicationEvent.channel=='email').all() if pids else []:
        if any(c.channel=='Email' and c.lapsed_policy_id==e.lapsed_policy_id and abs((c.occurred_at-e.created_at).total_seconds())<60 for c in rows):continue
        timeline.append(event(e.created_at,'Email','Event',e.details or 'Historical email event; the original message body was not recorded.',subject=e.campaign.subject if e.campaign else '',status=e.event_type,source='Historical campaign event'))
    timeline.sort(key=lambda item:item['at'])
    stored_files=ClientStoredFile.query.filter(ClientStoredFile.application_id.in_([a.id for a in applications])).filter(db.or_(ClientStoredFile.relative_path.like('fic-screening-%'),ClientStoredFile.relative_path.like('annexure_j1_%'))).all() if applications else []
    response = render_template('clients/file.html', identity=identity, searched=searched,
                               applications=applications, policies=policies, calls=calls, stored_files=stored_files,
                               scripts=scripts, documents=documents, messages=messages, timeline=timeline)
    return response, 200, {'Cache-Control': 'no-store'}


@client_files_bp.route('/document/<int:file_id>')
@login_required
@permission_required('recovery.view')
def stored_document(file_id):
    from app.models import ClientStoredFile
    from pathlib import PurePosixPath
    import mimetypes
    row=ClientStoredFile.query.get_or_404(file_id)
    a=ClientApplication.query.get_or_404(row.application_id)
    ensure_branch_access(a,agent_attr='agent_id')
    name=PurePosixPath(row.relative_path).name
    response=send_file(io.BytesIO(row.content),download_name=name,mimetype=mimetypes.guess_type(name)[0] or 'application/octet-stream',as_attachment=name.endswith('.json'),max_age=0)
    response.headers['Cache-Control']='no-store'
    return response


@client_files_bp.route('/communication', methods=['POST'])
@login_required
@permission_required('recovery.view')
def add_communication():
    from app.services.conversation_history import record_communication
    from datetime import datetime,timedelta
    aid=request.form.get('application_id',type=int);pid=request.form.get('policy_id',type=int)
    if bool(aid)==bool(pid):abort(400)
    if aid:ensure_branch_access(ClientApplication.query.get_or_404(aid),agent_attr='agent_id')
    else:ensure_branch_access(LapsedPolicy.query.get_or_404(pid),agent_attr='assigned_agent_id')
    try:
        channel=request.form.get('channel');direction=request.form.get('direction')
        body=(request.form.get('body') or '').strip();subject=(request.form.get('subject') or '').strip()
        at=datetime.fromisoformat(request.form.get('occurred_at',''))-timedelta(hours=2)
        if at.tzinfo or at>datetime.utcnow()+timedelta(minutes=5):raise ValueError('Enter a valid South African date and time, not a future time.')
        if channel not in {'Call','WhatsApp','Email'} or direction not in {'inbound','outbound'} or not 1<=len(body)<=20000 or len(subject)>255:raise ValueError('Choose the channel and direction and enter the discussion or message text.')
        record_communication(channel,body,'Recorded',application_id=aid,policy_id=pid,subject=subject,direction=direction,source='manual',occurred_at=at)
        db.session.commit();flash('Conversation entry saved.','success')
    except ValueError as exc:
        db.session.rollback();flash(str(exc),'danger')
    return redirect(url_for('client_files.index',application_id=aid,policy_id=pid,_anchor='chat-panel'))
