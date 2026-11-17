"""Source import, branch and communication dates for staff application views."""
import json
from sqlalchemy import func
from app import db
from app.models import (CampaignRecipient, CommunicationCampaign, ClientImportBatch, ClientImportCampaign,
                        WhatsAppMessage, ClientCommunication, BranchFilingOffice, AuditLog)


def application_sources(applications):
    ids=[a.source_campaign_recipient_id for a in applications if a.source_campaign_recipient_id]
    sources={row[0]:row for row in db.session.query(CampaignRecipient.id,CommunicationCampaign.branch,
        CommunicationCampaign.filing_branch,ClientImportBatch.description,ClientImportBatch.filename
        ).join(CommunicationCampaign,CommunicationCampaign.id==CampaignRecipient.campaign_id
        ).outerjoin(ClientImportCampaign,ClientImportCampaign.campaign_id==CommunicationCampaign.id
        ).outerjoin(ClientImportBatch,ClientImportBatch.id==ClientImportCampaign.batch_id
        ).filter(CampaignRecipient.id.in_(ids)).all()}
    invitation=dict(db.session.query(WhatsAppMessage.campaign_recipient_id,func.min(WhatsAppMessage.created_at)).filter(
        WhatsAppMessage.campaign_recipient_id.in_(ids),WhatsAppMessage.direction=='outbound',
        WhatsAppMessage.status.in_(['sent','delivered','read'])).group_by(WhatsAppMessage.campaign_recipient_id).all())
    emails={}
    for item in ClientCommunication.query.filter(ClientCommunication.application_id.in_([a.id for a in applications]),
            ClientCommunication.channel=='Email',ClientCommunication.subject.like('Signed documents received:%')
            ).order_by(ClientCommunication.occurred_at,ClientCommunication.id):
        emails[item.application_id]=item
    offices={o.branch_name.lower():o.email for o in BranchFilingOffice.query.filter_by(active=True)}
    attempts={}
    for item in AuditLog.query.filter_by(action='APPLICATION_SUBMISSION_EMAIL',entity_type='ClientApplication').filter(
            AuditLog.entity_id.in_([str(a.id) for a in applications])).order_by(AuditLog.created_at,AuditLog.id):
        try:
            value=json.loads(item.details or '{}')
            attempts.setdefault(int(item.entity_id),{})[value['to'].lower()]=dict(value,at=item.created_at)
        except (ValueError,TypeError,KeyError):
            continue
    result={}
    for a in applications:
        source=sources.get(a.source_campaign_recipient_id)
        policy=a.lapsed_policy
        filing=a.filing_branch or (source[2] if source else '') or ''
        email=emails.get(a.id)
        notices=attempts.get(a.id,{})
        branch_notice=notices.get(offices.get(filing.lower(),'').lower())
        result[a.id]=dict(branch=a.branch or '',source_branch=source[1] if source else (policy.branch if policy else ''),
            filing_branch=filing,email_to=offices.get(filing.lower(),''),
            import_description=source[3] if source and source[3] else '',
            filename=source[4] if source and source[4] else (policy.source_filename if policy else ''),
            invited_at=invitation.get(a.source_campaign_recipient_id),
            email_status=branch_notice['status'] if branch_notice else
                ('Legacy staff email: '+email.status+' (destination not recorded)' if email else
                 ('No recorded branch notification' if a.signed_at else 'Not submitted')),
            email_at=branch_notice['at'] if branch_notice else (email.occurred_at if email else None),
            email_attempts=list(notices.values()))
    return result
