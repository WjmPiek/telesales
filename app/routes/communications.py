from datetime import datetime, timedelta
import csv
import io
import secrets
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, jsonify, Response
from flask_login import login_required, current_user
from sqlalchemy import func
from app import db
from app.models import (
    CommunicationCampaign, CampaignRecipient, LapsedPolicy, AgentNotification,
    ContactSuppression, CommunicationFollowUp, CommunicationEvent, ClientApplication
)
from app.services.communication_service import (
    preference_for, is_suppressed, callback_links, record_callback,
    record_not_interested, record_opt_out
)
from app.services.email_service import send_email
from app.services.whatsapp_service import send_whatsapp_message
from app.services.branch_access import scope_by_branch

communications_bp = Blueprint("communications", __name__, url_prefix="/communications")


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
    text = f"{campaign.message_body}\n\nCall me back: {links['callback']}\nNot interested: {links['not_interested']}\nOpt out: {links['opt_out']}"
    if channel == "whatsapp":
        if not pref.whatsapp_allowed or not policy.cell_number:
            return False, "No permitted WhatsApp number"
        ok = send_whatsapp_message(policy.cell_number, text)
        recipient.whatsapp_status = "Sent" if ok else "Failed"
    else:
        if not pref.email_allowed or not policy.email_address:
            return False, "No permitted email address"
        html = render_template("communications/email_message.html", policy=policy, campaign=campaign, links=links)
        ok = send_email(policy.email_address, campaign.subject, text, html_body=html)
        recipient.email_status = "Sent" if ok else "Failed"
    _event(recipient, "sent" if ok else "failed", channel)
    return ok, None if ok else "Provider returned failure"


@communications_bp.route("/")
@login_required
def index():
    if not _is_manager():
        return redirect(url_for("communications.notifications"))
    campaigns = CommunicationCampaign.query.order_by(CommunicationCampaign.created_at.desc()).all()
    summary = {
        "campaigns": len(campaigns),
        "recipients": db.session.query(func.count(CampaignRecipient.id)).scalar() or 0,
        "callbacks": CampaignRecipient.query.filter_by(response_type="callback").count(),
        "opt_outs": CampaignRecipient.query.filter_by(response_type="opt_out").count(),
    }
    return render_template("communications/index.html", campaigns=campaigns, summary=summary)


@communications_bp.route("/new", methods=["GET", "POST"])
@login_required
def create_campaign():
    if not _is_manager(): abort(403)
    if request.method == "POST":
        campaign = CommunicationCampaign(
            name=(request.form.get("name") or "").strip(),
            subject=(request.form.get("subject") or "Funeral policy callback").strip(),
            message_body=(request.form.get("message_body") or "").strip(),
            send_whatsapp=bool(request.form.get("send_whatsapp")),
            send_email=bool(request.form.get("send_email")),
            branch=(request.form.get("branch") or current_user.branch or "").strip() or None,
            created_by_id=current_user.id,
        )
        if not campaign.name or not campaign.message_body:
            flash("Campaign name and message are required.", "danger")
            return render_template("communications/create.html")
        if not campaign.send_whatsapp and not campaign.send_email:
            flash("Select at least one delivery channel.", "danger")
            return render_template("communications/create.html")
        db.session.add(campaign)
        db.session.commit()
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
    if q:
        like = f"%{q}%"
        leads_query = leads_query.filter(db.or_(LapsedPolicy.surname.ilike(like), LapsedPolicy.cell_number.ilike(like), LapsedPolicy.email_address.ilike(like)))
    if status:
        leads_query = leads_query.filter(LapsedPolicy.recovery_status == status)
    leads = leads_query.filter(LapsedPolicy.recovery_status != "Opted Out").order_by(LapsedPolicy.imported_at.desc()).limit(500).all()
    metrics = {
        "total": len(recipients),
        "wa_sent": sum(1 for r in recipients if r.whatsapp_status == "Sent"),
        "email_sent": sum(1 for r in recipients if r.email_status == "Sent"),
        "callbacks": sum(1 for r in recipients if r.response_type == "callback"),
        "not_interested": sum(1 for r in recipients if r.response_type == "not_interested"),
        "opt_outs": sum(1 for r in recipients if r.response_type == "opt_out"),
    }
    return render_template("communications/view.html", campaign=campaign, recipients=recipients, leads=leads, metrics=metrics)


@communications_bp.route("/<int:campaign_id>/duplicate", methods=["POST"])
@login_required
def duplicate_campaign(campaign_id):
    if not _is_manager(): abort(403)
    source = CommunicationCampaign.query.get_or_404(campaign_id)
    clone = CommunicationCampaign(name=f"Copy of {source.name}", subject=source.subject, message_body=source.message_body,
        send_whatsapp=source.send_whatsapp, send_email=source.send_email, branch=source.branch,
        created_by_id=current_user.id, status="Draft")
    db.session.add(clone); db.session.flush()
    if request.form.get("copy_recipients"):
        for r in source.recipients:
            db.session.add(CampaignRecipient(campaign_id=clone.id, lapsed_policy_id=r.lapsed_policy_id, secure_token=secrets.token_urlsafe(32)))
    db.session.commit()
    flash("Campaign duplicated.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=clone.id))


@communications_bp.route("/<int:campaign_id>/archive", methods=["POST"])
@login_required
def archive_campaign(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    campaign.status = "Archived"
    db.session.commit()
    flash("Campaign archived.", "success")
    return redirect(url_for("communications.index"))


@communications_bp.route("/<int:campaign_id>/add-recipients", methods=["POST"])
@login_required
def add_recipients(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    ids = [int(x) for x in request.form.getlist("policy_ids") if x.isdigit()]
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
    flash(f"{added} recipient(s) added.", "success")
    return redirect(url_for("communications.view_campaign", campaign_id=campaign.id))


@communications_bp.route("/<int:campaign_id>/send", methods=["POST"])
@login_required
def send_campaign(campaign_id):
    if not _is_manager(): abort(403)
    campaign = CommunicationCampaign.query.get_or_404(campaign_id)
    sent_email = sent_whatsapp = 0
    for recipient in campaign.recipients:
        if campaign.send_whatsapp and recipient.whatsapp_status in {None, "Not Sent", "Failed"}:
            ok, _ = _send_to_recipient(campaign, recipient, "whatsapp"); sent_whatsapp += int(ok)
        if campaign.send_email and recipient.email_status in {None, "Not Sent", "Failed"}:
            ok, _ = _send_to_recipient(campaign, recipient, "email"); sent_email += int(ok)
    campaign.status = "Sent"; campaign.sent_at = datetime.utcnow()
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
