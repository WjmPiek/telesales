"""Import -> template -> preview -> selection -> delivery results."""
import io
import json
import secrets
from datetime import datetime
from pathlib import PurePosixPath
from zipfile import BadZipFile
from openpyxl.utils.exceptions import InvalidFileException

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for, jsonify, send_file
from flask_login import login_required, current_user
from openpyxl import load_workbook
from app import db
from app.models import (ClientImportBatch, ClientImportRow, ClientImportCampaign, CompanyGroupState, LapsedPolicy,
    CommunicationCampaign, CampaignRecipient, WhatsAppTemplate, PolicyProduct, BranchFilingOffice, AuditLog)
from app.security import permission_required
from app.services.client_directory import scoped_policies
from app.services.campaign_schedule import parse_schedule, schedule_sast
from app.services.whatsapp_clients import client_rows, visible_campaigns

sales_bp = Blueprint('sales', __name__, url_prefix='/sales')


@sales_bp.before_request
def protect():
    if current_user.is_authenticated:
        session.setdefault('sales_csrf', secrets.token_urlsafe(32))
        if request.method == 'POST':
            token = request.form.get('sales_csrf', '')
            if not token.isascii() or not secrets.compare_digest(session['sales_csrf'], token):
                abort(400, 'Refresh this page before continuing.')
            if request.content_length and request.content_length > 11 * 1024 * 1024:
                abort(413)


def companies():
    from app.routes.communications import _available_companies
    from app.services.franchise_routing import key
    return [c for c in _available_companies() if key(c.company_name) in
            {'martinsbrokers','brokers','martinsnorthcliff','martinsfuneralsnorthcliff','northcliff'} and c.status == 'Active']


def batch_record(batch_id):
    batch = ClientImportBatch.query.get_or_404(batch_id)
    if batch.company_id not in {c.id for c in companies()}:
        abort(403)
    return batch


def batch_policies(batch):
    return scoped_policies().join(ClientImportRow, ClientImportRow.policy_id == LapsedPolicy.id).filter(
        ClientImportRow.batch_id == batch.id, LapsedPolicy.company_id == batch.company_id)


def office_for(company):
    from app.services.franchise_routing import key
    offices = BranchFilingOffice.query.filter_by(active=True).all()
    exact = [o for o in offices if key(o.branch_name) in {key(company.branch), key(company.company_name)}]
    if len(exact) == 1:
        return exact[0]
    # Reuse the branch's existing routing, never another branch's last choice.
    for branch, in db.session.query(CommunicationCampaign.filing_branch).filter(
            CommunicationCampaign.company_id == company.id, CommunicationCampaign.filing_branch.isnot(None)
            ).order_by(CommunicationCampaign.id.desc()):
        match = next((o for o in offices if o.branch_name == branch), None)
        if match:
            return match
    return None


def render(page, **context):
    template=context.get('template')
    if template:
        try:
            context['template_buttons']=json.loads(template.buttons_json or '[]')
        except (ValueError,TypeError):
            context['template_buttons']=[]
        context['template_image']=template.header_image_url or (template.campaign.image_url if template.campaign else None)
    return render_template('sales/' + page + '.html', schedule_sast=schedule_sast, **context), 200, {'Cache-Control':'no-store'}


@sales_bp.route('/imports', methods=['GET','POST'])
@login_required
@permission_required('recovery.view')
def imports():
    allowed = companies()
    if request.method == 'POST':
        if not current_user.has_permission('recovery.import'):
            from app.security import is_admin_user
            if not is_admin_user():
                abort(403)
        company = next((c for c in allowed if c.id == request.form.get('company_id',type=int)), None)
        description = request.form.get('description','').strip()
        upload = request.files.get('file')
        try:
            if not company or not description or len(description)>255:
                raise ValueError('Select Northcliff or Brokers and enter a description (up to 255 characters).')
            if not upload or not (upload.filename or '').lower().endswith('.xlsx'):
                raise ValueError('Choose your Excel .xlsx client file.')
            content = upload.stream.read(10*1024*1024+1)
            if len(content)>10*1024*1024:
                raise ValueError('The file must be 10 MB or smaller.')
            filename = PurePosixPath(upload.filename.replace('\\','/')).name[:255]
            # Serialize file lookup and creation for this company in the same transaction.
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(db.update(CompanyGroupState).where(CompanyGroupState.id == company.id)
                    .values(updated_at=CompanyGroupState.updated_at))
            company = db.session.get(CompanyGroupState, company.id, populate_existing=True, with_for_update=True)
            if not company or company.status != 'Active':
                raise ValueError('This company is no longer active. Select an active company to import clients.')
            existing_batch = ClientImportBatch.query.filter_by(company_id=company.id, filename=filename,
                file_size=len(content), file_data=content).order_by(ClientImportBatch.id).first()
            if existing_batch:
                flash('This file has already been imported. Opened the saved import.', 'info')
                return redirect(url_for('.clients', batch_id=existing_batch.id))
            workbook = load_workbook(io.BytesIO(content),data_only=True,read_only=True)
            skipped = ''
            try:
                from app.services.laps_report import REPORT_HEADERS, header, import_report, skipped_rows_summary
                headers = [header(c.value) for c in workbook.active[1]]
                if REPORT_HEADERS.issubset(headers):
                    policies = []
                    _, _, _, missing, duplicates = import_report(workbook, filename, current_user,
                        company_override=company, commit=False, policies_out=policies)
                    skipped = skipped_rows_summary(missing, duplicates)
                elif headers == ['name','cellnumber']:
                    policies = import_names(workbook, company, filename)
                else:
                    raise ValueError('Use your laps report columns, or a file with Name and Cell_Number columns.')
            finally:
                workbook.close()
            office = office_for(company)
            batch = ClientImportBatch(filename=filename,description=description,company_id=company.id,
                filing_branch=office.branch_name if office else None, created_by_id=current_user.id,
                file_data=content,file_size=len(content))
            db.session.add(batch);db.session.flush()
            db.session.add_all([ClientImportRow(batch_id=batch.id,policy_id=p.id) for p in policies])
            db.session.add(AuditLog(user_id=current_user.id,action='CLIENT_FILE_IMPORTED',entity_type='ClientImportBatch',
                entity_id=str(batch.id),details=f'{len(policies)} records saved to company #{company.id}'))
            db.session.commit()
            if skipped:
                flash(f'{skipped} The original Excel file is available to download.', 'info')
            return redirect(url_for('.clients',batch_id=batch.id))
        except (ValueError, OSError, KeyError, BadZipFile, InvalidFileException) as exc:
            db.session.rollback();flash(str(exc),'danger')
    batches = ClientImportBatch.query.filter(ClientImportBatch.company_id.in_([c.id for c in allowed])).order_by(ClientImportBatch.id.desc()).all()
    legacy = (scoped_policies().with_entities(LapsedPolicy.source_filename,LapsedPolicy.company_id,
        db.func.count(LapsedPolicy.id)).filter(LapsedPolicy.source_filename.isnot(None),
        LapsedPolicy.company_id.in_([c.id for c in allowed]),
        ~LapsedPolicy.id.in_(db.session.query(ClientImportRow.policy_id)))
        .group_by(LapsedPolicy.source_filename,LapsedPolicy.company_id).all())
    return render('imports',companies=allowed,batches=batches,legacy=legacy)


def import_names(workbook, company, filename):
    from app.routes.prospects import prospect_phone
    from app.services.communication_service import normalize_phone
    existing = {normalize_phone(p.cell_number):p for p in LapsedPolicy.query.filter_by(company_id=company.id)}
    policies, seen = [], set()
    for index, row in enumerate(workbook.active.iter_rows(min_row=2,values_only=True),2):
        if index>10001:
            raise ValueError('Import at most 10,000 clients per file.')
        if not any(v is not None and str(v).strip() for v in row):
            continue
        name = str(row[0] or '').strip();phone = prospect_phone(row[1] if len(row)>1 else None)
        if not name or not phone:
            raise ValueError(f'Row {index}: enter the client name and a valid SA cell number.')
        first,_,surname = name.rpartition(' ')
        first = first or surname;surname = surname if ' ' in name else ''
        if len(first)>50 or len(surname)>120:
            raise ValueError(f'Row {index}: the client name is too long.')
        key = normalize_phone(phone)
        if key in seen:
            continue
        seen.add(key)
        policy = existing.get(key)
        if not policy:
            policy = LapsedPolicy(initials=first,surname=surname,cell_number=phone,lead_type='prospect',
                company_id=company.id,company_name=company.company_name,franchise=company.company_name,
                branch=company.branch,assigned_agent_id=current_user.id,recovery_status='New',
                source_type='prospect_import',source_filename=filename)
            db.session.add(policy);db.session.flush();existing[key]=policy
        policies.append(policy)
    if not policies:
        raise ValueError('There are no client rows in this file.')
    return policies


@sales_bp.route('/imports/open-existing',methods=['POST'])
@login_required
@permission_required('recovery.view')
def open_existing():
    company = next((c for c in companies() if c.id==request.form.get('company_id',type=int)),None)
    if not company:
        abort(403)
    filename = request.form.get('filename','')
    existing=ClientImportBatch.query.filter_by(company_id=company.id,filename=filename).order_by(ClientImportBatch.id.desc()).first()
    if existing:
        return redirect(url_for('.clients',batch_id=existing.id))
    policies = scoped_policies().filter_by(company_id=company.id,source_filename=filename).all()
    if not policies:
        abort(404)
    office=office_for(company)
    batch=ClientImportBatch(filename=filename,description=filename,company_id=company.id,
        filing_branch=office.branch_name if office else None,created_by_id=current_user.id)
    db.session.add(batch);db.session.flush()
    db.session.add_all([ClientImportRow(batch_id=batch.id,policy_id=p.id) for p in policies])
    db.session.commit()
    return redirect(url_for('.clients',batch_id=batch.id))


@sales_bp.route('/imports/<int:batch_id>')
@login_required
@permission_required('recovery.view')
def clients(batch_id):
    batch=batch_record(batch_id)
    query=batch_policies(batch)
    # The dedicated file page keeps all original client records, including duplicates.
    from sqlalchemy.orm import undefer
    policies=query.options(undefer(LapsedPolicy.source_row_json)).order_by(LapsedPolicy.id).all()
    sources={}
    for policy in policies:
        try:
            value=json.loads(policy.source_row_json or '{}')
            sources[policy.id]=value if isinstance(value,dict) else {}
        except (ValueError,TypeError):
            sources[policy.id]={}
    from app.services.whatsapp_clients import directory_delivery
    campaigns = CommunicationCampaign.query.join(ClientImportCampaign).filter(
        ClientImportCampaign.batch_id==batch.id, CommunicationCampaign.deleted_at.is_(None)).order_by(CommunicationCampaign.id.desc()).all()
    return render('clients',batch=batch,policies=policies,delivery=directory_delivery(policies),sources=sources,
                  send_batches=[(c,send_progress(c)) for c in campaigns],call_mode=request.args.get('mode')=='call')


@sales_bp.route('/imports/<int:batch_id>/download')
@login_required
def download(batch_id):
    batch=batch_record(batch_id)
    if not batch.file_size:
        abort(404)
    if batch_policies(batch).count() != ClientImportRow.query.filter_by(batch_id=batch.id).count():
        abort(403)
    return send_file(io.BytesIO(batch.file_data),download_name=batch.filename,as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',max_age=0)


@sales_bp.route('/imports/<int:batch_id>/templates')
@login_required
def templates(batch_id):
    batch=batch_record(batch_id)
    records=WhatsAppTemplate.query.filter_by(status='Approved').order_by(WhatsAppTemplate.name).all()
    return render('templates',batch=batch,templates=records)


def draft_values(batch, template):
    audience=request.form.get('audience','group')
    product_id=request.form.get('product_id', '').strip()
    product=PolicyProduct.query.filter_by(id=request.form.get('product_id',type=int),active=True).first() if product_id else None
    if audience not in {'individual','group'} or (product_id and not product):
        raise ValueError('Choose a valid audience or policy product.')
    if not batch.filing_branch or not BranchFilingOffice.query.filter_by(branch_name=batch.filing_branch,active=True).first():
        raise ValueError('This branch needs a document recipient. Configure its branch filing destination before sending.')
    return audience,product


@sales_bp.route('/imports/<int:batch_id>/campaign/<int:template_id>',methods=['GET','POST'])
@login_required
def campaign_setup(batch_id,template_id):
    batch=batch_record(batch_id)
    template=WhatsAppTemplate.query.filter_by(id=template_id,status='Approved').first_or_404()
    if request.method=='POST':
        try:
            audience,product=draft_values(batch,template)
            session['sales_preview']={'batch':batch.id,'template':template.id,'audience':audience,'product':product.id if product else None,'nonce':secrets.token_urlsafe(24)}
            return render('preview',batch=batch,template=template,audience=audience,product=product)
        except ValueError as exc:
            flash(str(exc),'danger')
    return render('setup',batch=batch,template=template)


@sales_bp.route('/imports/<int:batch_id>/submit',methods=['POST'])
@login_required
def submit(batch_id):
    batch=batch_record(batch_id)
    draft=session.get('sales_preview',{})
    if draft.get('batch')!=batch.id or draft.get('nonce')!=request.form.get('nonce'):
        abort(400,'Preview this file before submitting.')
    template=WhatsAppTemplate.query.filter_by(id=draft.get('template'),status='Approved').first_or_404()
    product=PolicyProduct.query.filter_by(id=draft['product'],active=True).first_or_404() if draft.get('product') else None
    source=template.campaign
    campaign=CommunicationCampaign(name=batch.description[:160],subject=batch.description,message_body=template.body_text,
        company_id=batch.company_id,branch=batch.company.branch,filing_branch=batch.filing_branch,
        product_id=product.id if product else None,audience_type=draft['audience'],created_by_id=current_user.id,
        whatsapp_template_name=template.name,whatsapp_template_language=template.language,
        template_category=template.category,template_status='Approved',template_provider_id=template.provider_template_id,
        template_buttons_json=template.buttons_json,template_footer=template.footer_text,
        image_data=source.image_data if source else None,image_filename=source.image_filename if source else None,
        image_mimetype=source.image_mimetype if source else None,
        image_url=template.header_image_url or (source.image_url if source else None),
        send_whatsapp=True,send_email=False,public_application_token=secrets.token_urlsafe(32))
    db.session.add(campaign);db.session.flush()
    if campaign.image_data:
        campaign.image_url=url_for('communications.campaign_image',campaign_id=campaign.id,_external=True)
    db.session.add(ClientImportCampaign(batch_id=batch.id,campaign_id=campaign.id));db.session.commit()
    session.pop('sales_preview',None)
    return redirect(url_for('.select',campaign_id=campaign.id))


def campaign_record(campaign_id):
    link=ClientImportCampaign.query.filter_by(campaign_id=campaign_id).first_or_404()
    batch=batch_record(link.batch_id)
    campaign=CommunicationCampaign.query.get_or_404(campaign_id)
    if campaign.company_id!=batch.company_id or campaign.deleted_at or campaign.status=='Archived':
        abort(404)
    return batch,campaign


@sales_bp.route('/campaign/<int:campaign_id>/select',methods=['GET','POST'])
@login_required
def select(campaign_id):
    batch,campaign=campaign_record(campaign_id)
    from app.services.campaign_selection import select_recipients
    from app.routes.communications import _refresh_template_status
    if request.method=='POST':
        try:
            if campaign.queue_status in {'queued','retry','processing'}:
                raise ValueError('This list is already sending or scheduled. View the results before sending again.')
            ids={int(v) for v in request.form.getlist('policy_ids') if v.isdigit()}
            policies=batch_policies(batch).filter(LapsedPolicy.id.in_(ids)).all()
            if not ids or {p.id for p in policies}!=ids:
                raise ValueError('Select clients from this imported file.')
            action=request.form.get('action')
            at=parse_schedule(request.form.get('scheduled_at','')) if action=='schedule' else datetime.utcnow()
            if action not in {'send','schedule'} or (action=='schedule' and at<=datetime.utcnow()):
                raise ValueError('Choose a future schedule date and time in SAST.')
            live=_refresh_template_status(campaign)
            if not live.ok or campaign.template_status!='Approved':
                raise ValueError(live.error or 'The WhatsApp template is not currently approved.')
            campaign=db.session.query(CommunicationCampaign).filter_by(id=campaign.id).with_for_update().populate_existing().one()
            if campaign.queue_status in {'queued','retry','processing'}:
                raise ValueError('This list is already queued.')
            db.session.expire(campaign,['recipients'])
            chosen=select_recipients(campaign,policies)
            if not chosen:
                raise ValueError('No eligible clients remain in this selection. Check opt-outs and previous sends.')
            campaign.status='Scheduled';campaign.queue_status='queued';campaign.scheduled_at=at
            db.session.commit()
            return render('progress',batch=batch,campaign=campaign,scheduled=action=='schedule',count=len(chosen))
        except ValueError as exc:
            db.session.rollback();flash(str(exc),'danger')
    from app.services.campaign_audience import audience_rows
    policies,states=audience_rows(batch_policies(batch).order_by(LapsedPolicy.id).all(),campaign.recipients)
    policies=[p for p in policies if not states[p.id].get('failed')]
    return render('select',batch=batch,campaign=campaign,policies=policies,states=states)


@sales_bp.route('/campaign/<int:campaign_id>/progress')
@login_required
def progress(campaign_id):
    batch,campaign=campaign_record(campaign_id)
    return jsonify(**send_progress(campaign),results=url_for('communications.whatsapp_clients',batch_id=batch.id),
                   manage=url_for('.send_status',campaign_id=campaign.id))


def send_progress(campaign):
    from app.services.campaign_send_lock import sender_active
    recipients=list(campaign.recipients)
    interrupted=campaign.queue_status=='processing' and not sender_active(campaign)
    return dict(done=campaign.queue_status not in {'queued','retry','processing'} or interrupted,
        interrupted=interrupted, queue_status=campaign.queue_status,
        sent=sum(r.whatsapp_status in {'Sent','Delivered','Read'} for r in recipients),
        failed=sum(r.whatsapp_status=='Failed' for r in recipients),
        held=sum(r.whatsapp_status in {'Held','Skipped'} for r in recipients),
        unsent=sum(r.whatsapp_status in {None,'Not Sent'} for r in recipients),
        review=sum(r.whatsapp_status=='Sending' for r in recipients),total=len(recipients))


@sales_bp.route('/campaign/<int:campaign_id>/status')
@login_required
def send_status(campaign_id):
    batch,campaign=campaign_record(campaign_id)
    return render('send_status',batch=batch,campaign=campaign,progress=send_progress(campaign))


@sales_bp.route('/campaign/<int:campaign_id>/resume',methods=['POST'])
@login_required
def resume_unsent(campaign_id):
    from app.services.campaign_send_lock import campaign_send_lock
    from app.services.campaign_selection import queued_recipients
    from app.routes.communications import _refresh_template_status
    from app.models import WhatsAppMessage
    batch,campaign=campaign_record(campaign_id)
    try:
        with campaign_send_lock(campaign.id) as acquired:
            if not acquired:
                raise ValueError('Messages are still sending. No second sender was started.')
            campaign=CommunicationCampaign.query.filter_by(id=campaign.id).populate_existing().with_for_update().one()
            if campaign.queue_status in {'queued','retry'}:
                raise ValueError('This batch is already queued or scheduled.')
            if batch.company.status!='Active':
                raise ValueError('This branch is not active.')
            recipients=queued_recipients(campaign)
            permitted={p.id for p in batch_policies(batch).all()}
            if any(r.lapsed_policy_id not in permitted for r in recipients):
                abort(403)
            accepted={rid for rid, in db.session.query(WhatsAppMessage.campaign_recipient_id).filter(
                WhatsAppMessage.campaign_recipient_id.in_([r.id for r in recipients]),
                WhatsAppMessage.direction=='outbound',WhatsAppMessage.status.in_(['sent','delivered','read']))}
            pending=[r.id for r in recipients if r.whatsapp_status in {None,'Not Sent'} and r.id not in accepted]
            if not pending:
                raise ValueError('There are no unsent messages to resume. Failed messages can be retried from Message results.')
            live=_refresh_template_status(campaign)
            if not live.ok or campaign.template_status!='Approved':
                raise ValueError(live.error or 'The WhatsApp template must be approved before resuming.')
            campaign.send_recipient_ids_json=json.dumps(pending)
            campaign.queue_status='queued';campaign.status='Scheduled';campaign.scheduled_at=datetime.utcnow()
            db.session.add(AuditLog(user_id=current_user.id,action='CAMPAIGN_UNSENT_RESUMED',entity_type='CommunicationCampaign',
                entity_id=str(campaign.id),details=f'{len(pending)} previously unsent messages queued'))
            db.session.commit()
            flash(f'{len(pending)} unsent messages queued. Successful messages will not be resent.','success')
    except ValueError as exc:
        db.session.rollback();flash(str(exc),'warning')
    return redirect(url_for('.send_status',campaign_id=campaign.id))


@sales_bp.route('/imports/<int:batch_id>/retry',methods=['POST'])
@login_required
def retry_failed(batch_id):
    batch=batch_record(batch_id)
    from app.routes.communications import _refresh_template_status
    from app.services.campaign_selection import select_recipients
    grouped={}
    query=CampaignRecipient.query.join(CommunicationCampaign).filter(
        CampaignRecipient.lapsed_policy_id.in_(batch_policies(batch).with_entities(LapsedPolicy.id)),
        CampaignRecipient.whatsapp_status.in_(['Failed','Held']),
        CommunicationCampaign.company_id==batch.company_id,CommunicationCampaign.deleted_at.is_(None))
    from app.services.whatsapp_clients import message_date_range
    from app.models import WhatsAppMessage
    try:
        low,high=message_date_range(request.form)
    except ValueError as exc:
        abort(400,str(exc))
    if low or high:
        messages=db.session.query(WhatsAppMessage.campaign_recipient_id).filter(WhatsAppMessage.direction=='outbound')
        if low: messages=messages.filter(WhatsAppMessage.created_at>=low)
        if high: messages=messages.filter(WhatsAppMessage.created_at<high)
        query=query.filter(CampaignRecipient.id.in_(messages))
    rows=query.order_by(CampaignRecipient.id.desc()).all()
    seen=set()
    from app.services.campaign_audience import contact_number
    for row in rows:
        number=contact_number(row.policy)
        if not number or number in seen:
            continue
        seen.add(number)
        grouped.setdefault(row.campaign_id,[]).append(row.policy)
    queued=0
    for campaign_id,policies in grouped.items():
        campaign=CommunicationCampaign.query.filter_by(id=campaign_id).with_for_update().populate_existing().one()
        if campaign.queue_status in {'queued','retry','processing'} or campaign.status=='Archived':
            continue
        live=_refresh_template_status(campaign)
        if not live.ok or campaign.template_status!='Approved':
            continue
        chosen=select_recipients(campaign,policies)
        if chosen:
            campaign.status='Scheduled';campaign.queue_status='queued';campaign.scheduled_at=datetime.utcnow()
            queued+=len(chosen)
    db.session.commit()
    flash(f'{queued} failed message(s) queued for retry. Opted-out and unavailable clients were excluded.','success' if queued else 'warning')
    return redirect(url_for('communications.whatsapp_clients',batch_id=batch.id,status='failed'))
