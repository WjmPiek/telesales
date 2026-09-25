"""Super-admin reset of operational test data.

Configuration is deliberately outside this reset: users, roles, policy products,
system settings, WhatsApp template definitions and suppression hashes survive.
"""
from pathlib import Path
import shutil

from flask import current_app

from app import db
from app.models import (
    AgentNotification,
    ApplicationCDD,
    ApplicationJourney,
    ApplicationMarketingConsent,
    ApplicationScreening,
    ApplicationSignature,
    AuditLog,
    CallRecording,
    CallSummary,
    CampaignRecipient,
    ClientApplication,
    ClientCommunication,
    ClientFicaDocument,
    ClientStoredFile,
    CommunicationCampaign,
    CommunicationEvent,
    CommunicationFollowUp,
    ComplianceFlag,
    ComplianceReview,
    ContactCommunicationPreference,
    ContactSuppression,
    DocumentSignature,
    HistoricalMemberCover,
    LapsedPolicy,
    RecoveryCallLog,
    SupportingDocumentReminder,
    TelesalesScriptSession,
    WhatsAppAuditEvent,
    WhatsAppContact,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppProviderJob,
    WhatsAppProviderLog,
    WhatsAppTemplate,
    WhatsAppWebhookEvent,
)
from app.services.communication_service import contact_hash, normalize_email, normalize_phone


RESET_COUNTS = {
    "applications": ClientApplication,
    "application_files": ClientStoredFile,
    "imported_clients": LapsedPolicy,
    "historical_member_covers": HistoricalMemberCover,
    "call_records": RecoveryCallLog,
    "script_sessions": TelesalesScriptSession,
    "campaign_recipients": CampaignRecipient,
    "whatsapp_contacts": WhatsAppContact,
    "whatsapp_conversations": WhatsAppConversation,
    "whatsapp_messages": WhatsAppMessage,
}


def reset_preview():
    counts = {label: model.query.count() for label, model in RESET_COUNTS.items()}
    counts["campaign_templates_kept"] = WhatsAppTemplate.query.count()
    counts["suppression_records_kept"] = ContactSuppression.query.count()
    return counts


def _add_suppression(phone, email, source, reason):
    phone_digest = contact_hash(normalize_phone(phone))
    email_digest = contact_hash(normalize_email(email))
    if not phone_digest and not email_digest:
        return
    existing = ContactSuppression.query.filter(
        db.or_(
            ContactSuppression.phone_hash == phone_digest if phone_digest else db.false(),
            ContactSuppression.email_hash == email_digest if email_digest else db.false(),
        )
    ).first()
    if existing is None:
        db.session.add(ContactSuppression(
            phone_hash=phone_digest,
            email_hash=email_digest,
            source=source,
            reason=reason,
        ))


def _preserve_opt_outs():
    for pref in ContactCommunicationPreference.query.filter_by(opted_out_all=True).all():
        policy = db.session.get(LapsedPolicy, pref.lapsed_policy_id)
        if policy:
            _add_suppression(policy.cell_number, policy.email_address,
                             pref.opt_out_source or "data_reset", "Client opted out")
    for contact in WhatsAppContact.query.filter_by(opted_out=True).all():
        _add_suppression(contact.phone_number, contact.email,
                         "whatsapp", "Client opted out")
    for consent in ApplicationMarketingConsent.query.filter_by(allowed=False).all():
        application = db.session.get(ClientApplication, consent.application_id)
        if application:
            _add_suppression(application.cell_number, application.email,
                             "popia", "POPIA marketing consent declined")
    db.session.flush()
    ContactSuppression.query.update({
        ContactSuppression.campaign_id: None,
        ContactSuppression.lapsed_policy_id: None,
    }, synchronize_session=False)


def _remove_client_document_cache():
    root = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    clients = (root / "clients").resolve()
    if clients.parent != root:
        raise RuntimeError("Unsafe client document path")
    if clients.exists():
        shutil.rmtree(clients)


def reset_operational_data():
    """Delete operational/imported data in one transaction and return prior counts."""
    counts = reset_preview()
    _preserve_opt_outs()

    ApplicationJourney.query.delete(synchronize_session=False)
    ApplicationScreening.query.delete(synchronize_session=False)
    ApplicationCDD.query.delete(synchronize_session=False)
    ApplicationMarketingConsent.query.delete(synchronize_session=False)
    ClientStoredFile.query.delete(synchronize_session=False)
    DocumentSignature.query.delete(synchronize_session=False)
    ClientFicaDocument.query.delete(synchronize_session=False)
    ApplicationSignature.query.delete(synchronize_session=False)
    ComplianceReview.query.delete(synchronize_session=False)
    TelesalesScriptSession.query.delete(synchronize_session=False)
    ClientCommunication.query.delete(synchronize_session=False)
    SupportingDocumentReminder.query.delete(synchronize_session=False)
    ClientApplication.query.delete(synchronize_session=False)

    # Delivery/inbox history is removed. Template definitions, provider IDs and
    # header media remain available for the next import.
    WhatsAppMessage.query.delete(synchronize_session=False)
    WhatsAppConversation.query.delete(synchronize_session=False)
    WhatsAppContact.query.delete(synchronize_session=False)
    WhatsAppWebhookEvent.query.delete(synchronize_session=False)
    CommunicationFollowUp.query.delete(synchronize_session=False)
    CommunicationEvent.query.delete(synchronize_session=False)
    CampaignRecipient.query.delete(synchronize_session=False)
    WhatsAppProviderJob.query.delete(synchronize_session=False)
    WhatsAppProviderLog.query.delete(synchronize_session=False)
    WhatsAppAuditEvent.query.delete(synchronize_session=False)
    for campaign in CommunicationCampaign.query.all():
        campaign.sent_at = None
        campaign.scheduled_at = None
        campaign.queue_status = "idle"
        campaign.archived_at = None
        campaign.deleted_at = None

    CallSummary.query.delete(synchronize_session=False)
    ComplianceFlag.query.delete(synchronize_session=False)
    CallRecording.query.delete(synchronize_session=False)
    RecoveryCallLog.query.delete(synchronize_session=False)
    ContactCommunicationPreference.query.delete(synchronize_session=False)
    HistoricalMemberCover.query.delete(synchronize_session=False)
    LapsedPolicy.query.delete(synchronize_session=False)

    operational_entities = {
        "ClientApplication", "ClientFicaDocument", "LapsedPolicy",
        "TelesalesScriptSession", "CommunicationCampaign", "CampaignRecipient",
        "WhatsAppMessage", "WhatsAppConversation", "WhatsAppContact",
    }
    AuditLog.query.filter(AuditLog.entity_type.in_(operational_entities)).delete(
        synchronize_session=False)
    AgentNotification.query.filter(AgentNotification.entity_type.in_(operational_entities)).delete(
        synchronize_session=False)

    db.session.commit()
    try:
        _remove_client_document_cache()
    except Exception:
        current_app.logger.exception("Database reset succeeded but cached client files could not be removed")
    counts["suppression_records_kept"] = ContactSuppression.query.count()
    return counts
