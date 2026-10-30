from datetime import datetime

from app import db
from app.models import CommunicationCampaign, WhatsAppAuditEvent, AgentNotification


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


def process_scheduled_campaigns(limit=10):
    """Send due campaigns safely. Failed campaigns remain visible and retryable."""
    now = datetime.utcnow()
    campaigns = CommunicationCampaign.query.filter(
        CommunicationCampaign.status == "Scheduled",
        CommunicationCampaign.scheduled_at.isnot(None),
        CommunicationCampaign.scheduled_at <= now,
        CommunicationCampaign.queue_status.in_(["queued", "retry"]),
    ).order_by(CommunicationCampaign.scheduled_at.asc()).limit(limit).all()
    stats = {"processed": 0, "sent": 0, "failed": 0, "held": 0}
    for campaign in campaigns:
        claimed = CommunicationCampaign.query.filter(
            CommunicationCampaign.id == campaign.id,
            CommunicationCampaign.queue_status.in_(["queued", "retry"]),
        ).update({"queue_status": "processing"}, synchronize_session=False)
        db.session.commit()
        if not claimed:
            continue
        stats["processed"] += 1
        try:
            from app.routes.communications import _send_to_recipient, _refresh_template_status
            if campaign.send_whatsapp:
                result = _refresh_template_status(campaign)
                if not result.ok or campaign.template_status != "Approved":
                    raise RuntimeError(result.error or f"Template status is {campaign.template_status}")
            sent = 0
            for recipient in campaign.recipients:
                if campaign.send_whatsapp and recipient.whatsapp_status in {None, "Not Sent", "Failed", "Held"}:
                    ok, _ = _send_to_recipient(campaign, recipient, "whatsapp")
                    sent += int(ok)
                db.session.commit()
            held = sum(r.whatsapp_status == 'Held' for r in campaign.recipients)
            failed = sum(r.whatsapp_status == 'Failed' for r in campaign.recipients)
            needs_attention = bool(held or failed)
            campaign.status = "Needs attention" if needs_attention else "Sent"
            campaign.queue_status = "held" if held else ("failed" if failed else "completed")
            campaign.sent_at = datetime.utcnow() if sent else campaign.sent_at
            audit("campaign_auto_failed" if failed else ("campaign_auto_held" if held else "campaign_auto_sent"), "campaign", campaign.id,
                  campaign.created_by_id, f"{sent} sent, {failed} failed, {held} held")
            db.session.add(AgentNotification(user_id=campaign.created_by_id, title="Campaign needs attention" if needs_attention else "Scheduled campaign sent", message=f"{campaign.name}: {sent} sent, {failed} failed, {held} held. Review the campaign report for failure details before retrying.", notification_type="campaign_failed" if failed else ("campaign_held" if held else "campaign_sent"), entity_type="campaign", entity_id=campaign.id))
            db.session.commit()
            stats["failed" if failed else ("held" if held else "sent")] += 1
        except Exception as exc:
            db.session.rollback()
            campaign = db.session.get(CommunicationCampaign, campaign.id)
            campaign.status = "Scheduled"
            campaign.queue_status = "retry"
            audit("campaign_auto_send_failed", "campaign", campaign.id, campaign.created_by_id, str(exc))
            db.session.add(AgentNotification(user_id=campaign.created_by_id, title="Scheduled campaign needs attention", message=f"{campaign.name}: {exc}", notification_type="campaign_failed", entity_type="campaign", entity_id=campaign.id))
            db.session.commit()
            stats["failed"] += 1
    return stats

