"""Idempotent correction for the protected Brokers account and its reported QR case."""

from app import db
from app.models import AuditLog, ClientApplication, CommunicationCampaign, Role, User


OWNER_EMAIL = "wjm@martinsdirect.com"
REPORTED_APPLICATION = "QR-20260924-82C9E8"


def reconcile_brokers_branch():
    """Correct only Wjm Piek and the QR application reported by the user."""
    owner = User.query.filter(db.func.lower(User.email) == OWNER_EMAIL).first()
    if owner is None:
        return {"owner": False, "application": False, "campaign": False}

    changed = {"owner": False, "application": False, "campaign": False}
    super_role = Role.query.filter_by(name="Super Admin").first()
    if super_role is None:
        super_role = Role(name="Super Admin", description="Protected Super Admin account")
        db.session.add(super_role)
        db.session.flush()
    if owner.role_id != super_role.id or owner.branch != "Brokers" or not owner.active:
        owner.role = super_role
        owner.branch = "Brokers"
        owner.active = True
        changed["owner"] = True
        db.session.add(AuditLog(
            user_id=owner.id, action="Protected Super Admin branch corrected",
            entity_type="User", entity_id=str(owner.id),
            details="Protected Wjm Piek account assigned to Brokers and kept active as Super Admin."))

    application = ClientApplication.query.filter_by(application_ref=REPORTED_APPLICATION).first()
    if (application is not None and application.agent_id == owner.id
            and application.application_type == "New Policy - QR Campaign"
            and application.branch == "Alberton"):
        application.branch = "Brokers"
        if application.lapsed_policy and application.lapsed_policy.branch == "Alberton":
            application.lapsed_policy.branch = "Brokers"
        changed["application"] = True
        db.session.add(AuditLog(
            user_id=owner.id, action="QR application branch corrected",
            entity_type="ClientApplication", entity_id=str(application.id),
            details=f"{REPORTED_APPLICATION}: Alberton corrected to Brokers, including linked lead."))
        journey = application.whatsapp_journey
        campaign = db.session.get(CommunicationCampaign, journey.campaign_id) if journey and journey.campaign_id else None
        if campaign and campaign.created_by_id == owner.id and campaign.branch == "Alberton":
            campaign.branch = "Brokers"
            changed["campaign"] = True
            db.session.add(AuditLog(
                user_id=owner.id, action="QR campaign branch corrected",
                entity_type="CommunicationCampaign", entity_id=str(campaign.id),
                details=f"Campaign for {REPORTED_APPLICATION}: Alberton corrected to Brokers for future applications."))

    if any(changed.values()):
        db.session.commit()
    return changed
