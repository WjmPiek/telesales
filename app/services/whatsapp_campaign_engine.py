import json
import os
from datetime import datetime

from sqlalchemy import and_, or_

from app import db
from app.models import AgentNotification, CampaignRecipient, CommunicationCampaign, WhatsAppAuditEvent
from app.services.campaign_send_lock import campaign_send_lock


def audit(action, entity_type, entity_id=None, user_id=None, details=None):
    event = WhatsAppAuditEvent(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )
    db.session.add(event)
    return event


SENDABLE = (None, "Not Sent", "Failed", "Held")


def send_batch_size():
    try:
        size = int(os.getenv("WHATSAPP_SEND_BATCH_SIZE", "10"))
    except ValueError:
        size = 10
    return min(100, max(1, size))


def due_campaigns(now):
    # Only recover interrupted sends that already use a durable batch cursor.
    return CommunicationCampaign.query.filter(
        CommunicationCampaign.status == "Scheduled",
        CommunicationCampaign.deleted_at.is_(None),
        CommunicationCampaign.scheduled_at <= now,
        or_(CommunicationCampaign.queue_status.in_(["queued", "retry"]),
            and_(CommunicationCampaign.queue_status == "processing",
                 CommunicationCampaign.send_batch_cursor_id.isnot(None))),
    )


def process_scheduled_campaigns(limit=10):
    """Process one bounded batch per due campaign on each scheduler tick."""
    now = datetime.utcnow()
    ids = due_campaigns(now).with_entities(CommunicationCampaign.id).order_by(
        CommunicationCampaign.scheduled_at, CommunicationCampaign.id).limit(limit).all()
    stats = {"processed": 0, "sent": 0, "failed": 0, "held": 0}
    for campaign_id, in ids:
        with campaign_send_lock(campaign_id) as acquired:
            if acquired:
                _process_campaign(campaign_id, now, stats)
    return stats


def _selected_recipients(campaign):
    query = CampaignRecipient.query.filter_by(campaign_id=campaign.id)
    if campaign.send_recipient_ids_json is not None:
        # Invalid batch data must fail closed, never expand to the whole campaign.
        ids = json.loads(campaign.send_recipient_ids_json)
        if not isinstance(ids, list) or any(type(value) is not int for value in ids):
            raise ValueError("Invalid campaign client selection")
        query = query.filter(CampaignRecipient.id.in_(ids))
    return query


def _pending_recipients(campaign, selected):
    return selected.filter(
        CampaignRecipient.id > campaign.send_batch_cursor_id,
        or_(CampaignRecipient.whatsapp_status.is_(None), CampaignRecipient.whatsapp_status.in_(SENDABLE[1:])),
    )


def _lock_campaign(campaign_id):
    return CommunicationCampaign.query.filter_by(id=campaign_id).populate_existing().with_for_update().first()


def _sending(campaign):
    return campaign and campaign.status == "Scheduled" and campaign.queue_status == "processing" and not campaign.deleted_at


def _process_campaign(campaign_id, now, stats):
    campaign = due_campaigns(now).filter_by(id=campaign_id).populate_existing().with_for_update().first()
    if campaign is None:
        db.session.rollback()
        return
    campaign.queue_status = "processing"
    if campaign.send_batch_cursor_id is None:
        campaign.send_batch_cursor_id = 0
    db.session.commit()
    stats["processed"] += 1
    try:
        from app.routes.communications import _send_to_recipient, _refresh_template_status
        from app.services.campaign_audience import campaign_number_states, contact_number

        selected = _selected_recipients(campaign)
        if campaign.send_whatsapp:
            recipients = _pending_recipients(campaign, selected).order_by(CampaignRecipient.id).limit(send_batch_size()).all()
            if recipients:
                result = _refresh_template_status(campaign)
                if not result.ok or campaign.template_status != "Approved":
                    raise RuntimeError(result.error or f"Template status is {campaign.template_status}")
                numbers = {contact_number(recipient.policy) for recipient in recipients}
                matches = campaign_number_states(campaign.id, numbers)
                number_states = {number: matches.get(number) for number in numbers}
                for recipient in recipients:
                    campaign = _lock_campaign(campaign_id)
                    if not _sending(campaign):
                        db.session.rollback()
                        return
                    db.session.refresh(recipient, with_for_update=True)
                    campaign.send_batch_cursor_id = recipient.id
                    if recipient.whatsapp_status not in SENDABLE:
                        db.session.commit()
                        continue

                    # Persist the intent and cursor together. Recovery advances past
                    # an uncertain attempt even if the process dies inside the API call.
                    recipient.whatsapp_status = "Sending"
                    db.session.commit()
                    ok, _ = _send_to_recipient(campaign, recipient, "whatsapp", number_states=number_states)
                    if recipient.whatsapp_status == "Sending":
                        recipient.whatsapp_status = "Sent" if ok else "Failed"
                    if ok:
                        number_states[contact_number(recipient.policy)] = "Sent"
                        campaign.sent_at = datetime.utcnow()
                    db.session.commit()

        campaign = _lock_campaign(campaign_id)
        if not _sending(campaign):
            db.session.rollback()
            return
        if campaign.send_whatsapp and _pending_recipients(campaign, selected).with_entities(CampaignRecipient.id).first():
            campaign.queue_status = "queued"
            db.session.commit()
            return

        status = CampaignRecipient.whatsapp_status
        counts = dict(selected.with_entities(status, db.func.count(CampaignRecipient.id)).group_by(status))
        sent = sum(counts.get(key, 0) for key in ("Sent", "Delivered", "Read"))
        failed = counts.get("Failed", 0)
        held = counts.get("Held", 0) + counts.get("Sending", 0)
        needs_attention = bool(held or failed)
        campaign.status = "Needs attention" if needs_attention else "Sent"
        campaign.queue_status = "held" if held else ("failed" if failed else "completed")
        campaign.send_batch_cursor_id = None
        audit("campaign_auto_failed" if failed else ("campaign_auto_held" if held else "campaign_auto_sent"), "campaign", campaign.id,
              campaign.created_by_id, f"{sent} sent, {failed} failed, {held} held or need checking")
        db.session.add(AgentNotification(
            user_id=campaign.created_by_id, title="Campaign needs attention" if needs_attention else "Scheduled campaign sent",
            message=f"{campaign.name}: {sent} sent, {failed} failed, {held} held or need checking. Review the campaign report before retrying.",
            notification_type="campaign_failed" if failed else ("campaign_held" if held else "campaign_sent"),
            entity_type="campaign", entity_id=campaign.id,
        ))
        db.session.commit()
        stats["failed" if failed else ("held" if held else "sent")] += 1
    except Exception as exc:
        db.session.rollback()
        campaign = _lock_campaign(campaign_id)
        if not _sending(campaign):
            db.session.rollback()
            return
        campaign.status = "Needs attention"
        campaign.queue_status = "held"
        audit("campaign_auto_send_failed", "campaign", campaign.id, campaign.created_by_id, str(exc))
        db.session.add(AgentNotification(
            user_id=campaign.created_by_id, title="Scheduled campaign needs attention",
            message=f"{campaign.name}: {exc}", notification_type="campaign_failed", entity_type="campaign", entity_id=campaign.id,
        ))
        db.session.commit()
        stats["failed"] += 1
