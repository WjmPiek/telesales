from datetime import datetime, timedelta
import csv
import io
import secrets
import os
import json
from werkzeug.utils import secure_filename
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, jsonify, Response, send_file
from flask_login import login_required, current_user
from sqlalchemy import func
from app import db
from app.models import (
    CommunicationCampaign, CampaignRecipient, LapsedPolicy, AgentNotification,
    ContactSuppression, CommunicationFollowUp, CommunicationEvent, ClientApplication,
    WhatsAppTemplate, WhatsAppMediaAsset, WhatsAppProviderJob, WhatsAppMessage,
    WhatsAppMediaVersion, WhatsAppProviderLog, WhatsAppAuditEvent
)
from app.services.communication_service import (
    preference_for, is_suppressed, callback_links, record_callback,
    record_not_interested, record_opt_out
)
from app.services.email_service import send_email
from app.services.whatsapp_service import send_whatsapp_message, send_whatsapp_template_image, get_whatsapp_template_status, create_whatsapp_image_template, validate_public_image_url
from app.services.whatsapp_enterprise import submit_campaign_template, sync_campaign_template, queue_provider_job
from app.services.whatsapp_campaign_engine import audit
from app.services.branch_access import scope_by_branch

communications_bp = Blueprint("communications", __name__, url_prefix="/communications")




def _normalise_za_phone(value):
    """Return a South African mobile number in +27 international format."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if digits.startswith("0027"):
        digits = digits[2:]
    if digits.startswith("27") and len(digits) == 11:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+27" + digits[1:]
    if len(digits) == 9:
        return "+27" + digits
    return None


def _get_or_create_manual_policy(first_name, surname, phone):
    """Create a lightweight lead for an individual campaign, or reuse an existing lead."""
    normalised = _normalise_za_phone(phone)
    if not normalised:
        return None, "Enter a valid South African contact number, for example 0676200748."
    last_nine = normalised[-9:]
    policy = LapsedPolicy.query.filter(
        db.or_(
            LapsedPolicy.cell_number == normalised,
            LapsedPolicy.cell_number == "0" + last_nine,
            LapsedPolicy.cell_number.ilike(f"%{last_nine}"),
        )
    ).first()
    if policy:
        if first_name:
            policy.initials = first_name.strip()
        if surname:
            policy.surname = surname.strip()
        policy.cell_number = normalised
        return policy, None
    policy = LapsedPolicy(
        member_id=f"MANUAL-{datetime.utcnow():%Y%m%d%H%M%S}-{secrets.token_hex(3)}",
        initials=(first_name or "").strip(),
        surname=(surname or "").strip(),
        cell_number=normalised,
        branch=(current_user.branch or "").strip() or None,
        assigned_agent_id=current_user.id,
        recovery_status="New",
        comments="Created from an individual WhatsApp campaign.",
    )
    db.session.add(policy)
    db.session.flush()
    return policy, None


def _refresh_template_status(campaign):
    """Synchronise a campaign template with the live provider state."""
    previous_status = campaign.template_status
    result = get_whatsapp_template_status(
        campaign.whatsapp_template_name,
        campaign.whatsapp_template_language or "en_US",
    )
    campaign.template_checked_at = datetime.utcnow()
    campaign.template_status_error = result.error
    if result.ok:
        campaign.template_status = result.status
        if result.status == "Approved":
            if not campaign.template_approved_at:
                campaign.template_approved_at = datetime.utcnow()
            campaign.template_approved_by_id = None
            if previous_status != "Approved" and not campaign.template_approval_notified_at:
                db.session.add(AgentNotification(
                    user_id=campaign.created_by_id,
                    title="WhatsApp template approved",
                    message=f"Template {campaign.whatsapp_template_name} for campaign {campaign.name} is approved. You can now send the campaign.",
                    notification_type="whatsapp_template_approved",
                    entity_type="campaign",
                    entity_id=campaign.id,
                ))
                campaign.template_approval_notified_at = datetime.utcnow()
        else:
            campaign.template_approved_at = None
            campaign.template_approved_by_id = None
    elif result.status == "Not found":
        campaign.template_status = "Not found"
        campaign.template_approved_at = None
        campaign.template_approved_by_id = None
    db.session.commit()
    return result


def _is_manager():
    role = (current_user.role.name if current_user.role else "").lower()
    return role in {"admin", "super admin", "super_admin", "branch manager", "branch_manager", "manager", "supervisor"}


def _event(recipient, event_type, channel=None, details=None):
    db.session.add(CommunicationEvent(
        campaign_id=recipient.campaign_id,
        recipient_id=recipient.id,
        lapsed_policy_id=recipient.lapsed_policy_id,
        event_type=event_type,
        channel=channel,
        details=details,
    ))


def _send_to_recipient(campaign, recipient, channel):
    policy = recipient.policy
    pref = preference_for(policy)
    if pref.opted_out_all or is_suppressed(policy):
        _event(recipient, "suppressed", channel, "Contact is opted out or on suppression list")
        return False, "Suppressed"
    links = callback_links(recipient.secure_token)
    error = None
    text = f"{campaign.message_body}\n\nCall me back: {links['callback']}\nNot interested: {links['not_interested']}\nOpt out: {links['opt_out']}"
    if channel == "whatsapp":
        if not pref.whatsapp_allowed or not policy.cell_number:
            return False, "No permitted WhatsApp number"
        if campaign.whatsapp_template_name and campaign.image_url:
            result = send_whatsapp_template_image(
                policy.cell_number, campaign.whatsapp_template_name,
                campaign.whatsapp_template_language or "en_US", campaign.image_url,
                f"callback:{recipient.secure_token}", f"optout:{recipient.secure_token}",
                f"{policy.initials or ''} {policy.surname or ''}".strip() or "Customer",
            )
            ok = result.ok
            error = result.error
        else:
            ok = send_whatsapp_message(policy.cell_number, text)
            error = None if ok else "Provider returned failure"
        recipient.whatsapp_status = "Sent" if ok else "Failed"
    else:
        if not pref.email_allowed or not policy.email_address:
            return False, "No permitted email address"
        html = render_template("communications/email_message.html", policy=policy, campaign=campaign, links=links)
        ok = send_email(policy.email_address, campaign.subject, text, html_body=html)
        recipient.email_status = "Sent" if ok else "Failed"
        error = None if ok else "Email provider returned failure"
    _event(recipient, "sent" if ok else "failed", channel, None if ok else error)
    return ok, None if ok else (error or "Provider returned failure")


@communications_bp.route("/")
@login_required
def index():
    if not _is_manager():
        return redirect(url_for("communications.notifications"))
    show_archived = request.args.get("archived") == "1"
    query = CommunicationCampaign.query
    if not show_archived:
        query = query.filter(CommunicationCampaign.status != "Archived")
    campaigns = query.order_by(CommunicationCampaign.created_at.desc()).all()
    summary = {
        "campaigns": len(campaigns),
        "recipients": db.session.query(func.count(CampaignRecipient.id)).scalar() or 0,
        "callbacks": CampaignRecipient.query.filter_by(response_type="callback").count(),
        "opt_outs": CampaignRecipient.query.filter_by(response_type="opt_out").count(),
    }
    return render_template("communications/index.html", campaigns=campaigns, summary=summary)




@communications_bp.route("/whatsapp-dashboard")
@login_required
def whatsapp_dashboard():
    if not _is_manager(): abort(403)
    templates = WhatsAppTemplate.query.order_by(WhatsAppTemplate.created_at.desc()).limit(100).all()
    jobs = WhatsAppProviderJob.query.order_by(WhatsAppProviderJob.created_at.desc()).limit(25).all()
    stats = {
        "templates": WhatsAppTemplate.query.count(),
        "pending": WhatsAppTemplate.query.filter(WhatsAppTemplate.status.in_(["Pending", "Submitting"])).count(),
        "approved": WhatsAppTemplate.query.filter_by(status="Approved").count(),
        "rejected": WhatsAppTemplate.query.filter_by(status="Rejected").count(),
        "queued_jobs": WhatsAppProviderJob.query.filter_by(status="pending").count(),
        "sent_messages": WhatsAppMessage.query.filter_by(direction="outbound").count(),
        "delivered_messages": WhatsAppMessage.query.filter_by(direction="outbound", status="delivered").count(),
        "read_messages": WhatsAppMessage.query.filter_by(direction="outbound", status="read").count(),
        "scheduled": CommunicationCampaign.query.filter_by(status="Scheduled").count(),
        "queue_failed": CommunicationCampaign.query.filter_by(queue_status="retry").count(),
        "media_assets": WhatsAppMediaAsset.query.count(),
        "provider_errors": WhatsAppProviderLog.query.filter_by(status="failed").count(),
    }
    recent_audit = WhatsAppAuditEvent.query.order_by(WhatsAppAuditEvent.created_at.desc()).limit(20).all()
    provider_logs = WhatsAppProviderLog.query.order_by(WhatsAppProviderLog.created_at.desc()).limit(20).all()
    return render_template("communications/whatsapp_dashboard.html", templates=templates, jobs=jobs, stats=stats, recent_audit=recent_audit, provider_logs=provider_logs)


@communications_bp.route("/templates")
@login_required
def template_library():
    if not _is_manager(): abort(403)
    templates = WhatsAppTemplate.query.order_by(WhatsAppTemplate.updated_at.desc()).all()
    return render_template("communications/templates.html", templates=templates)


@communications_bp.route("/templates/<int:template_id>/sync", methods=["POST"])
@login_required
def sync_template_record(template_id):
    if not _is_manager(): abort(403)
    template = WhatsAppTemplate.query.get_or_404(template_id)
    campaign = CommunicationCampaign.query.get_or_404(template.campaign_id)
    ok, message = sync_campaign_template(campaign)
    flash(("Template synchronized: " if ok else "Template sync failed: ") + message, "success" if ok else "danger")
    return redirect(url_for("communications.template_library"))


@communications_bp.route("/templates/sync-all", methods=["POST"])
@login_required
def sync_all_templates():
    if not _is_manager(): abort(403)
    pending = WhatsAppTemplate.query.filter(WhatsAppTemplate.status.in_(["Pending", "Submitting", "Submission failed"])).all()
    count = 0
    for item in pending:
        campaign = db.session.get(CommunicationCampaign, item.campaign_id)
        if campaign:
            if item.status == "Submission failed":
                submit_campaign_template(campaign, force=True)
            else:
                sync_campaign_template(campaign)
            count += 1
    flash(f"Synchronized {count} WhatsApp template(s).", "success")
    return redirect(url_for("communications.template_library"))


@communications_bp.route("/new", methods=["GET", "POST"])
@login_required
def create_campaign():
    if not _is_manager(): abort(403)
    if request.method == "POST":
        image = request.files.get("campaign_image")
        image_filename = None
        image_url = None
        image_data = None
        image_mimetype = None
        if image and image.filename:
            ext = os.path.splitext(image.filename)[1].lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                flash("Campaign image must be JPG, PNG or WEBP.", "danger")
                return render_template("communications/create.html")
            image_filename = secure_filename(f"campaign_{datetime.utcnow():%Y%m%d%H%M%S}_{secrets.token_hex(5)}{ext}")
            image_data = image.read()
            if not image_data:
                flash("The uploaded campaign image was empty.", "danger")
                return render_template("communications/create.html")
            if len(image_data) > 12 * 1024 * 1024:
                flash("Campaign image is too large. Maximum size is 12 MB.", "danger")
                return render_template("communications/create.html")
            image_mimetype = image.mimetype or {".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png", ".webp":"image/webp"}.get(ext, "application/octet-stream")

        campaign_name = (request.form.get("name") or "").strip()
        message_body = (request.form.get("message_body") or "").strip()
        if "{{1}}" not in message_body:
            message_body = "Hello {{1}},\n\n" + message_body
        requested_template_name = (request.form.get("whatsapp_template_name") or "").strip().lower()
        if not requested_template_name and campaign_name:
            import re
            base_name = re.sub(r"[^a-z0-9]+", "_", campaign_name.lower()).strip("_") or "campaign"
            requested_template_name = f"{base_name}_{datetime.utcnow():%Y%m%d%H%M%S}"
        button_texts = request.form.getlist("button_text")
        button_types = request.form.getlist("button_type")
        button_values = request.form.getlist("button_value")
        template_buttons = []
        for index, text_value in enumerate(button_texts[:10]):
            text_value = (text_value or "").strip()
            if not text_value:
                continue
            btype = (button_types[index] if index < len(button_types) else "QUICK_REPLY").strip().upper()
            item = {"type": btype, "text": text_value}
            extra = (button_values[index] if index < len(button_values) else "").strip()
            if btype == "URL": item["url"] = extra
            if btype == "PHONE_NUMBER": item["phone_number"] = extra
            template_buttons.append(item)
        if not template_buttons:
            template_buttons = [
                {"type": "QUICK_REPLY", "text": "YES, CALL ME BACK"},
                {"type": "QUICK_REPLY", "text": "NO THANKS, OPT OUT"},
            ]
        campaign = CommunicationCampaign(
            name=campaign_name,
            subject=(request.form.get("subject") or "Funeral policy callback").strip(),
            message_body=message_body,
            whatsapp_template_name=requested_template_name or None,
            whatsapp_template_language=(request.form.get("whatsapp_template_language") or "en").strip(),
            template_category=(request.form.get("template_category") or "MARKETING").strip().upper(),
            template_type="MEDIA_INTERACTIVE",
            template_footer=(request.form.get("template_footer") or "").strip()[:60] or None,
            template_buttons_json=json.dumps(template_buttons),
            template_allow_category_change=request.form.get("allow_category_change") == "1",
            image_filename=image_filename, image_url=image_url, image_data=image_data, image_mimetype=image_mimetype,
            audience_type=(request.form.get("audience_type") or "group").strip().lower(),
            send_whatsapp=bool(request.form.get("send_whatsapp")),
            send_email=bool(request.form.get("send_email")),
            branch=(request.form.get("branch") or current_user.branch or "").strip() or None,
            created_by_id=current_user.id,
        )
        if campaign.audience_type not in {"individual", "group"}:
            campaign.audience_type = "group"
        if not campaign.name or not campaign.message_body:
            flash("Campaign name and message are required.", "danger")
            return render_template("communications/create.html")
        # During creation image_url is assigned only after the campaign has an ID.
        # Validate the uploaded binary here, not image_url, otherwise every new
        # WhatsApp image campaign is rejected before it can be saved.
        if campaign.send_whatsapp and (not campaign.whatsapp_template_name or not campaign.image_data):
            missing = []
            if not campaign.image_data:
                missing.append("advert image")
            if not campaign.whatsapp_template_name:
                missing.append("campaign name so a template name can be generated")
            flash("Please provide the " + " and ".join(missing) + " for this WhatsApp image campaign.", "danger")
            return render_template("communications/create.html")
        if not campaign.send_whatsapp and not campaign.send_email:
            flash("Select at least one delivery channel.", "danger")
            return render_template("communications/create.html")
        db.session.add(campaign)
        db.session.commit()
        if campaign.image_data:
            campaign.image_url = request.url_root.rstrip("/") + url_for("communications.campaign_image", campaign_id=campaign.id)

        template_mode = (request.form.get("template_mode") or "create").strip().lower()
        if campaign.send_whatsapp and template_mode == "create":
            ok, submit_message = submit_campaign_template(campaign)
            if not ok:
                queue_provider_job("submit_template", campaign.id, delay_seconds=120, max_attempts=5)
                flash(f"Campaign saved. Automatic template submission will retry: {submit_message}", "warning")

        individual_recipient = None
        if campaign.audience_type == "individual":
            policy, phone_error = _get_or_create_manual_policy(
                request.form.get("individual_first_name"),
                request.form.get("individual_surname"),
                request.form.get("individual_phone"),
            )
            if phone_error:
                db.session.rollback()
                db.session.delete(campaign)
                db.session.commit()
                flash(phone_error, "danger")
                return render_template("communications/create.html")
            if is_suppressed(policy) or preference_for(policy).opted_out_all:
                db.session.rollback()
                db.session.delete(campaign)
                db.session.commit()
                flash("This number is opted out or suppressed and cannot receive marketing messages.", "danger")
                return render_template("communications/create.html")
            individual_recipient = CampaignRecipient(
                campaign_id=campaign.id,
                lapsed_policy_id=policy.id,
                secure_token=secrets.token_urlsafe(32),
            )
            db.session.add(individual_recipient)
            db.session.flush()
            _event(individual_recipient, "recipient_added", details="Added from the individual campaign form")

        db.session.commit()

        quick_send = campaign.audience_type == "individual" and request.form.get("action") == "send_now"
        if quick_send and individual_recipient:
            result = _refresh_template_status(campaign)
            if result.ok and campaign.template_status == "Approved":
                ok, error = _send_to_recipient(campaign, individual_recipient, "whatsapp")
                if ok:
                    campaign.status = "Sent"
                    campaign.sent_at = datetime.utcnow()
                    db.session.commit()
                    flash("Campaign created and sent to the individual client.", "success")
                else:
                    db.session.commit()
                    flash(f"Campaign created, but the message could not be sent: {error or 'Provider returned failure'}", "danger")
            else:
                reason = result.error or f"Current live status is {campaign.template_status}."
                flash(f"Campaign and client were saved, but sending is blocked until the template is Approved. {reason}", "warning")
        else:
            flash("Campaign created. Add recipients and send it from the campaign page.", "success")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    return render_template("communications/create.html")


@communications_bp.route("/<int:campaign_id>")
@login_required
def view_campaign(campaign_id):
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    recipients = CampaignRecipient.query.filter_by(campaign_id=campaign.id).order_by(CampaignRecipient.id.desc()).all()
    leads_query = scope_by_branch(LapsedPolicy.query, LapsedPolicy, agent_col=LapsedPolicy.assigned_agent_id)
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    branch = (request.args.get("branch") or "").strip()
    if q:
        like = f"%{q}%"
        leads_query = leads_query.filter(db.or_(LapsedPolicy.surname.ilike(like), LapsedPolicy.cell_number.ilike(like), LapsedPolicy.email_address.ilike(like)))
    if status:
        leads_query = leads_query.filter(LapsedPolicy.recovery_status == status)
    if branch:
        leads_query = leads_query.filter(LapsedPolicy.branch == branch)
    leads = leads_query.filter(LapsedPolicy.recovery_status != "Opted Out").order_by(LapsedPolicy.imported_at.desc()).limit(500).all()
    metrics = {
        "total": len(recipients),
        "wa_sent": sum(1 for r in recipients if r.whatsapp_status == "Sent"),
        "email_sent": sum(1 for r in recipients if r.email_status == "Sent"),
        "callbacks": sum(1 for r in recipients if r.response_type == "callback"),
        "not_interested": sum(1 for r in recipients if r.response_type == "not_interested"),
        "opt_outs": sum(1 for r in recipients if r.response_type == "opt_out"),
    }
    branches = [row[0] for row in db.session.query(LapsedPolicy.branch).filter(LapsedPolicy.branch.isnot(None), LapsedPolicy.branch != "").distinct().order_by(LapsedPolicy.branch).all()]
    return render_template("communications/view.html", campaign=campaign, recipients=recipients, leads=leads, metrics=metrics, branches=branches)



@communications_bp.route("/media/<int:campaign_id>/<path:filename>")
def campaign_media_file(campaign_id, filename):
    """Stable extension-bearing HTTPS URL for Meta/360dialog media validation."""
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    if not campaign.image_data:
        abort(404)
    response = send_file(
        io.BytesIO(campaign.image_data),
        mimetype=campaign.image_mimetype or "image/jpeg",
        download_name=campaign.image_filename or filename,
        max_age=86400,
        conditional=True,
    )
    response.headers["Content-Disposition"] = "inline"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@communications_bp.route("/<int:campaign_id>/image")
def campaign_image(campaign_id):
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    if campaign.image_data:
        return send_file(
            io.BytesIO(campaign.image_data),
            mimetype=campaign.image_mimetype or "application/octet-stream",
            download_name=campaign.image_filename or f"campaign-{campaign.id}.jpg",
            max_age=3600,
            conditional=True,
        )
    if campaign.image_url:
        return redirect(campaign.image_url)
    abort(404)


@communications_bp.route("/<int:campaign_id>/image/replace", methods=["POST"])
@login_required
def replace_campaign_image(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    image = request.files.get("campaign_image")
    if not image or not image.filename:
        flash("Choose a JPG, PNG or WEBP image.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    ext = os.path.splitext(image.filename)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        flash("Campaign image must be JPG, PNG or WEBP.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    data = image.read()
    if not data or len(data) > 12 * 1024 * 1024:
        flash("Image is empty or larger than 12 MB.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    campaign.image_filename = secure_filename(f"campaign_{datetime.utcnow():%Y%m%d%H%M%S}_{secrets.token_hex(5)}{ext}")
    campaign.image_data = data
    campaign.image_mimetype = image.mimetype or {".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png", ".webp":"image/webp"}.get(ext)
    campaign.image_url = request.url_root.rstrip("/") + url_for("communications.campaign_image", campaign_id=campaign.id)
    db.session.commit()
    flash("Campaign image replaced and stored safely in the database.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/<int:campaign_id>/duplicate", methods=["POST"])
@login_required
def duplicate_campaign(campaign_id):
    if not _is_manager(): abort(403)
    source = CommunicationCampaign.query.get_or_404(campaign_id)
    clone = CommunicationCampaign(name=f"Copy of {source.name}", subject=source.subject, message_body=source.message_body,
        whatsapp_template_name=source.whatsapp_template_name, whatsapp_template_language=source.whatsapp_template_language,
        image_filename=source.image_filename, image_url=source.image_url, image_data=source.image_data, image_mimetype=source.image_mimetype, audience_type=source.audience_type or "group",
        template_status="Pending", template_approved_at=None, template_approved_by_id=None,
        send_whatsapp=source.send_whatsapp, send_email=source.send_email, branch=source.branch,
        created_by_id=current_user.id, status="Draft")
    db.session.add(clone); db.session.flush()
    if request.form.get("copy_recipients"):
        for r in source.recipients:
            db.session.add(CampaignRecipient(campaign_id=clone.id, lapsed_policy_id=r.lapsed_policy_id, secure_token=secrets.token_urlsafe(32)))
    db.session.commit()
    flash("Campaign duplicated.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=clone.id))


@communications_bp.route("/<int:campaign_id>/template-settings", methods=["POST"])
@login_required
def update_template_settings(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    name = (request.form.get("whatsapp_template_name") or "").strip()
    language = (request.form.get("whatsapp_template_language") or "en_US").strip()
    if not name:
        flash("Enter the exact 360dialog template name.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    campaign.whatsapp_template_name = name
    campaign.whatsapp_template_language = language
    campaign.template_status = "Pending"
    campaign.template_checked_at = None
    campaign.template_status_error = None
    campaign.template_approved_at = None
    campaign.template_approved_by_id = None
    db.session.commit()
    result = _refresh_template_status(campaign)
    if result.status == "Approved":
        flash("Template details updated. The template is approved and sending is enabled.", "success")
    else:
        flash(f"Template details updated. Current live status: {result.status}.", "warning")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))




@communications_bp.route("/<int:campaign_id>/template/create", methods=["POST"])
@login_required
def create_campaign_template(campaign_id):
    """Submit the campaign's image template to 360dialog/Meta for approval."""
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    if not campaign.send_whatsapp:
        flash("This campaign is not configured for WhatsApp.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    if not campaign.image_data or not campaign.image_url:
        flash("Upload the campaign image before creating the WhatsApp template.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    # Always rebuild an absolute public URL from BASE_URL so Meta never receives
    # a localhost, internal Render hostname or login-protected relative path.
    public_base = (os.getenv("BASE_URL") or request.url_root).rstrip("/")
    public_image_url = public_base + url_for("communications.campaign_image", campaign_id=campaign.id)
    campaign.image_url = public_image_url
    # Do not make the Render web service call itself while using a single sync
    # Gunicorn worker. That self-request can deadlock until the 25-second timeout.
    # The campaign image is already stored in PostgreSQL and served by the public
    # campaign_image route, so only perform an external reachability test when the
    # image host differs from the current request host.
    from urllib.parse import urlparse
    image_host = (urlparse(public_image_url).netloc or "").lower()
    request_host = (request.host or "").lower()
    if image_host and request_host and image_host != request_host:
        image_ok, image_error = validate_public_image_url(public_image_url)
        if not image_ok:
            campaign.template_status = "Submission failed"
            campaign.template_status_error = image_error
            campaign.template_checked_at = datetime.utcnow()
            db.session.commit()
            flash(f"Template could not be submitted: {image_error}", "danger")
            return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))

    ok, message = submit_campaign_template(campaign, force=True)
    if ok:
        flash("Template submitted automatically to 360dialog/Meta. TeleSales will monitor it until approved.", "success")
    else:
        queue_provider_job("submit_template", campaign.id, delay_seconds=120, max_attempts=5)
        flash(f"Template submission is queued for automatic retry: {message}", "warning")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/<int:campaign_id>/template-status", methods=["POST"])
@login_required
def update_template_status(campaign_id):
    """Backward-compatible form endpoint: now performs a live provider check."""
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    result = _refresh_template_status(campaign)
    if result.ok:
        if result.status == "Approved":
            flash("Template is Approved and active. The Send button is now available.", "success")
        else:
            flash(f"Live template status: {result.status}. Sending remains blocked.", "warning")
    else:
        flash(f"Could not confirm the template status: {result.error}", "danger")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/<int:campaign_id>/template-status/check", methods=["POST"])
@login_required
def check_template_status(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    result = _refresh_template_status(campaign)
    template = result.template or {}
    components = template.get("components") if isinstance(template, dict) else None
    return jsonify({
        "ok": result.ok,
        "status": campaign.template_status,
        "raw_status": result.raw_status,
        "approved": campaign.template_status == "Approved",
        "checked_at": campaign.template_checked_at.isoformat() if campaign.template_checked_at else None,
        "submitted_at": campaign.template_submitted_at.isoformat() if campaign.template_submitted_at else None,
        "approved_at": campaign.template_approved_at.isoformat() if campaign.template_approved_at else None,
        "error": result.error,
        "diagnostics": {
            "provider": result.provider or os.getenv("WHATSAPP_PROVIDER", "360dialog"),
            "provider_request_id": result.provider_request_id,
            "template_id": result.template_id,
            "template_name": campaign.whatsapp_template_name,
            "category": result.category or (template.get("category") if isinstance(template, dict) else None),
            "language": result.language or campaign.whatsapp_template_language,
            "rejection_reason": result.rejection_reason,
            "quality_score": result.quality_score,
            "image_url": campaign.image_url or (request.url_root.rstrip("/") + url_for("communications.campaign_image", campaign_id=campaign.id)),
            "components": components if isinstance(components, list) else [],
        },
    }), (200 if result.ok else 422)


@communications_bp.route("/<int:campaign_id>/delete", methods=["POST"])
@login_required
def delete_campaign(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    confirmation = (request.form.get("confirm_name") or "").strip()
    if confirmation != campaign.name:
        flash("Campaign was not deleted. Enter the exact campaign name to confirm permanent deletion.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))

    image_path = None
    if campaign.image_filename:
        image_path = os.path.join(current_app.root_path, "static", "uploads", "campaigns", campaign.image_filename)

    # Query only recipient IDs. Do not load campaign.recipients and then bulk-delete
    # those rows, because SQLAlchemy would retain stale recipient objects in the
    # identity map and try to UPDATE them while deleting the parent campaign.
    recipient_ids = [row[0] for row in db.session.query(CampaignRecipient.id).filter_by(campaign_id=campaign.id).all()]

    try:
        # Keep suppression history for POPIA compliance, but remove the deleted campaign link.
        ContactSuppression.query.filter_by(campaign_id=campaign.id).update(
            {ContactSuppression.campaign_id: None}, synchronize_session=False
        )
        if recipient_ids:
            CommunicationFollowUp.query.filter(
                CommunicationFollowUp.recipient_id.in_(recipient_ids)
            ).delete(synchronize_session=False)
            CommunicationEvent.query.filter(
                CommunicationEvent.recipient_id.in_(recipient_ids)
            ).delete(synchronize_session=False)

        CommunicationEvent.query.filter_by(campaign_id=campaign.id).delete(synchronize_session=False)
        CommunicationFollowUp.query.filter_by(campaign_id=campaign.id).delete(synchronize_session=False)
        CampaignRecipient.query.filter_by(campaign_id=campaign.id).delete(synchronize_session=False)
        AgentNotification.query.filter_by(
            entity_type="campaign", entity_id=campaign.id
        ).delete(synchronize_session=False)
        WhatsAppProviderJob.query.filter_by(campaign_id=campaign.id).delete(synchronize_session=False)
        WhatsAppMediaAsset.query.filter_by(campaign_id=campaign.id).delete(synchronize_session=False)
        WhatsAppTemplate.query.filter_by(campaign_id=campaign.id).delete(synchronize_session=False)

        # Clear any relationship state that may have been populated by an earlier
        # request hook or template helper before deleting the parent row.
        if "recipients" in campaign.__dict__:
            db.session.expire(campaign, ["recipients"])

        db.session.delete(campaign)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not delete communication campaign %s", campaign.id)
        flash("The campaign could not be deleted because related records are still being updated. Please try again.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))

    if image_path and os.path.isfile(image_path):
        try:
            os.remove(image_path)
        except OSError:
            current_app.logger.warning("Could not remove campaign image %s", image_path)
    flash("Campaign permanently deleted. Opt-out and suppression history was retained.", "success")
    return redirect(url_for("communications.index"))


@communications_bp.route("/<int:campaign_id>/archive", methods=["POST"])
@login_required
def archive_campaign(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    campaign.status = "Archived"
    campaign.archived_at = datetime.utcnow()
    campaign.queue_status = "idle"
    audit("campaign_archived", "campaign", campaign.id, current_user.id, campaign.name)
    db.session.commit()
    flash("Campaign archived.", "success")
    return redirect(url_for("communications.index"))


@communications_bp.route("/<int:campaign_id>/add-recipients", methods=["POST"])
@login_required
def add_recipients(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    ids = [int(x) for x in request.form.getlist("policy_ids") if x.isdigit()]
    if campaign.audience_type == "individual" and len(ids) > 1:
        ids = ids[:1]
    if campaign.audience_type == "individual" and ids:
        CampaignRecipient.query.filter_by(campaign_id=campaign.id).delete(synchronize_session=False)
    added = 0
    for policy in LapsedPolicy.query.filter(LapsedPolicy.id.in_(ids)).all():
        if is_suppressed(policy) or preference_for(policy).opted_out_all:
            continue
        exists = CampaignRecipient.query.filter_by(campaign_id=campaign.id, lapsed_policy_id=policy.id).first()
        if not exists:
            recipient = CampaignRecipient(campaign_id=campaign.id, lapsed_policy_id=policy.id, secure_token=secrets.token_urlsafe(32))
            db.session.add(recipient); db.session.flush(); _event(recipient, "recipient_added")
            added += 1
    db.session.commit()
    if campaign.audience_type == "individual" and added:
        flash("Individual client selected.", "success")
    else:
        flash(f"{added} recipient(s) added.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/<int:campaign_id>/add-filtered-group", methods=["POST"])
@login_required
def add_filtered_group(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    if campaign.audience_type != "group":
        flash("This campaign is configured for one individual client.", "warning")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    leads_query = scope_by_branch(LapsedPolicy.query, LapsedPolicy, agent_col=LapsedPolicy.assigned_agent_id)
    q = (request.form.get("q") or "").strip()
    status = (request.form.get("status") or "").strip()
    branch = (request.form.get("branch") or "").strip()
    if q:
        like = f"%{q}%"
        leads_query = leads_query.filter(db.or_(LapsedPolicy.surname.ilike(like), LapsedPolicy.cell_number.ilike(like), LapsedPolicy.email_address.ilike(like)))
    if status:
        leads_query = leads_query.filter(LapsedPolicy.recovery_status == status)
    if branch:
        leads_query = leads_query.filter(LapsedPolicy.branch == branch)
    policies = leads_query.filter(LapsedPolicy.recovery_status != "Opted Out").order_by(LapsedPolicy.imported_at.desc()).limit(2000).all()
    added = skipped = 0
    for policy in policies:
        if is_suppressed(policy) or preference_for(policy).opted_out_all:
            skipped += 1
            continue
        exists = CampaignRecipient.query.filter_by(campaign_id=campaign.id, lapsed_policy_id=policy.id).first()
        if exists:
            skipped += 1
            continue
        recipient = CampaignRecipient(campaign_id=campaign.id, lapsed_policy_id=policy.id, secure_token=secrets.token_urlsafe(32))
        db.session.add(recipient); db.session.flush(); _event(recipient, "recipient_added")
        added += 1
    db.session.commit()
    flash(f"Group selection complete: {added} added, {skipped} excluded or already selected.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id, q=q, status=status, branch=branch))


@communications_bp.route("/<int:campaign_id>/send", methods=["POST"])
@login_required
def send_campaign(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    if campaign.send_whatsapp:
        result = _refresh_template_status(campaign)
        if not result.ok or campaign.template_status != "Approved":
            reason = result.error or f"Current live status is {campaign.template_status}."
            flash(f"Sending blocked: the template is not Approved and active in 360dialog. {reason}", "danger")
            return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    sent_email = sent_whatsapp = 0
    for recipient in campaign.recipients:
        if campaign.send_whatsapp and recipient.whatsapp_status in {None, "Not Sent", "Failed"}:
            ok, _ = _send_to_recipient(campaign, recipient, "whatsapp"); sent_whatsapp += int(ok)
        if campaign.send_email and recipient.email_status in {None, "Not Sent", "Failed"}:
            ok, _ = _send_to_recipient(campaign, recipient, "email"); sent_email += int(ok)
    campaign.status = "Sent"; campaign.sent_at = datetime.utcnow(); campaign.queue_status = "completed"
    audit("campaign_sent", "campaign", campaign.id, current_user.id, f"WhatsApp {sent_whatsapp}; Email {sent_email}")
    db.session.commit()
    flash(f"Campaign processed: {sent_whatsapp} WhatsApp and {sent_email} email message(s) sent.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/<int:campaign_id>/schedule-follow-up", methods=["POST"])
@login_required
def schedule_follow_up(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    days = max(1, min(30, int(request.form.get("days") or 3)))
    channel = request.form.get("channel") or "email"
    due_at = datetime.utcnow() + timedelta(days=days)
    count = 0
    for recipient in campaign.recipients:
        if recipient.response_type:
            continue
        exists = CommunicationFollowUp.query.filter_by(recipient_id=recipient.id, channel=channel, status="Pending").first()
        if not exists:
            db.session.add(CommunicationFollowUp(campaign_id=campaign.id, recipient_id=recipient.id, due_at=due_at, channel=channel)); count += 1
    db.session.commit()
    flash(f"{count} follow-up message(s) scheduled for {days} day(s) from now.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/<int:campaign_id>/report")
@login_required
def campaign_report(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    recipients = campaign.recipients
    application_lead_ids = [r.lapsed_policy_id for r in recipients]
    applications = ClientApplication.query.filter(ClientApplication.lapsed_policy_id.in_(application_lead_ids)).all() if application_lead_ids else []
    metrics = {
        "recipients": len(recipients),
        "wa_sent": sum(r.whatsapp_status == "Sent" for r in recipients),
        "email_sent": sum(r.email_status == "Sent" for r in recipients),
        "callbacks": sum(r.response_type == "callback" for r in recipients),
        "not_interested": sum(r.response_type == "not_interested" for r in recipients),
        "opt_outs": sum(r.response_type == "opt_out" for r in recipients),
        "applications": len(applications),
    }
    metrics["callback_rate"] = round((metrics["callbacks"] / metrics["recipients"] * 100), 1) if metrics["recipients"] else 0
    metrics["application_rate"] = round((metrics["applications"] / metrics["recipients"] * 100), 1) if metrics["recipients"] else 0
    events = CommunicationEvent.query.filter_by(campaign_id=campaign.id).order_by(CommunicationEvent.created_at.desc()).limit(300).all()
    return render_template("communications/report.html", campaign=campaign, metrics=metrics, events=events)


@communications_bp.route("/<int:campaign_id>/export.csv")
@login_required
def export_campaign(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(["Client", "Cell", "Email", "WhatsApp", "Email status", "Response", "Channel", "Responded at"])
    for r in campaign.recipients:
        writer.writerow([f"{r.policy.initials or ''} {r.policy.surname or ''}".strip(), r.policy.cell_number or "", r.policy.email_address or "", r.whatsapp_status, r.email_status, r.response_type or "", r.response_channel or "", r.responded_at or ""])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=campaign_{campaign.id}_report.csv"})


@communications_bp.route("/respond/<token>/<action>")
def public_response(token, action):
    recipient = CampaignRecipient.query.filter_by(secure_token=token).first_or_404()
    channel = (request.args.get("channel") or "link").lower()
    if action == "callback":
        record_callback(recipient, channel); _event(recipient, "callback", channel)
        message = "Thank you. A consultant will call you back."
    elif action == "not-interested":
        record_not_interested(recipient, channel); _event(recipient, "not_interested", channel)
        message = "Thank you. We recorded that you are not interested in this campaign."
    elif action == "opt-out":
        record_opt_out(recipient, channel); _event(recipient, "opt_out", channel)
        message = "Your opt-out has been recorded. You will not receive further marketing communication."
    else: abort(404)
    db.session.commit()
    return render_template("communications/response.html", message=message)


@communications_bp.route("/notifications")
@login_required
def notifications():
    items = AgentNotification.query.filter_by(user_id=current_user.id).order_by(AgentNotification.created_at.desc()).limit(100).all()
    return render_template("communications/notifications.html", items=items)


@communications_bp.route("/notifications/read-all", methods=["POST"])
@login_required
def read_all_notifications():
    AgentNotification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit(); flash("Notifications marked as read.", "success")
    return redirect(url_for("communications.notifications"))


@communications_bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def read_notification(notification_id):
    item = AgentNotification.query.filter_by(id=notification_id, user_id=current_user.id).first_or_404()
    item.is_read = True; db.session.commit()
    return redirect(url_for("recovery.log_call", policy_id=item.entity_id)) if item.entity_type == "LapsedPolicy" else redirect(url_for("communications.notifications"))


@communications_bp.route("/suppression")
@login_required
def suppression_list():
    if not _is_manager(): abort(403)
    items = ContactSuppression.query.order_by(ContactSuppression.suppressed_at.desc()).limit(500).all()
    return render_template("communications/suppression.html", items=items)


@communications_bp.route("/<int:campaign_id>/schedule", methods=["POST"])
@login_required
def schedule_campaign(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    raw = (request.form.get("scheduled_at") or "").strip()
    try:
        scheduled = datetime.fromisoformat(raw)
    except ValueError:
        flash("Choose a valid campaign date and time.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    if scheduled <= datetime.utcnow():
        flash("Scheduled time must be in the future.", "danger")
        return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))
    campaign.scheduled_at = scheduled
    campaign.status = "Scheduled"
    campaign.queue_status = "queued"
    audit("campaign_scheduled", "campaign", campaign.id, current_user.id, scheduled.isoformat())
    db.session.commit()
    flash("Campaign added to the automatic send queue.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/<int:campaign_id>/queue/<action>", methods=["POST"])
@login_required
def campaign_queue_action(campaign_id, action):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    if action == "pause":
        campaign.queue_status = "paused"; campaign.status = "Paused"
    elif action == "resume":
        campaign.queue_status = "queued"; campaign.status = "Scheduled"
    elif action == "retry":
        campaign.queue_status = "retry"; campaign.status = "Scheduled"
        campaign.scheduled_at = datetime.utcnow()
    else:
        abort(404)
    audit(f"campaign_{action}", "campaign", campaign.id, current_user.id, campaign.name)
    db.session.commit()
    flash(f"Campaign queue action completed: {action}.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/media")
@login_required
def media_library():
    if not _is_manager(): abort(403)
    assets = WhatsAppMediaAsset.query.order_by(WhatsAppMediaAsset.updated_at.desc()).all()
    return render_template("communications/media_library.html", assets=assets)


@communications_bp.route("/media/<int:asset_id>/version/<int:version_id>")
@login_required
def media_version_file(asset_id, version_id):
    version = WhatsAppMediaVersion.query.filter_by(id=version_id, media_asset_id=asset_id).first_or_404()
    if not version.file_data: abort(404)
    return Response(version.file_data, mimetype=version.mime_type or "application/octet-stream", headers={"Cache-Control":"public, max-age=31536000, immutable"})


@communications_bp.route("/audit")
@login_required
def whatsapp_audit():
    if not _is_manager(): abort(403)
    events = WhatsAppAuditEvent.query.order_by(WhatsAppAuditEvent.created_at.desc()).limit(500).all()
    logs = WhatsAppProviderLog.query.order_by(WhatsAppProviderLog.created_at.desc()).limit(500).all()
    return render_template("communications/whatsapp_audit.html", events=events, logs=logs)


@communications_bp.route("/webhooks/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        verify_token = current_app.config.get("WHATSAPP_VERIFY_TOKEN") or __import__('os').getenv("WHATSAPP_VERIFY_TOKEN")
        if request.args.get("hub.verify_token") == verify_token:
            return request.args.get("hub.challenge", "")
        abort(403)
    payload = request.get_json(silent=True) or {}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in (change.get("value", {}) or {}).get("messages", []):
                interactive = msg.get("interactive") or {}
                text = ((msg.get("text") or {}).get("body") or (msg.get("button") or {}).get("payload") or (interactive.get("button_reply") or {}).get("id") or "").strip()
                upper = text.upper()
                for prefix, action in (("CALLBACK:", "callback"), ("NOTINTERESTED:", "not-interested"), ("OPTOUT:", "opt-out"), ("STOP:", "opt-out")):
                    if upper.startswith(prefix):
                        token = text.split(":", 1)[1].strip()
                        recipient = CampaignRecipient.query.filter_by(secure_token=token).first()
                        if recipient:
                            {"callback": record_callback, "not-interested": record_not_interested, "opt-out": record_opt_out}[action](recipient, "whatsapp")
                            _event(recipient, action.replace("-", "_"), "whatsapp")
                            db.session.commit()
    return jsonify({"ok": True})
