"""Enterprise WhatsApp template/media orchestration for TeleSales.

Meta Cloud API is the transport provider; TeleSales owns the operational state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
import time
from typing import Any

import requests
from flask import current_app

from app import db
from app.models import (
    AgentNotification,
    CommunicationCampaign,
    WhatsAppTemplate,
    WhatsAppMediaAsset,
    WhatsAppProviderJob,
    WhatsAppMediaVersion,
    WhatsAppProviderLog,
)
from app.services.whatsapp_service import (
    create_whatsapp_image_template,
    get_whatsapp_template_status,
)


@dataclass
class MediaPublishResult:
    ok: bool
    url: str | None = None
    provider: str = "database"
    provider_id: str | None = None
    error: str | None = None


def _public_campaign_image_url(campaign: CommunicationCampaign) -> str:
    base = (os.getenv("BASE_URL") or current_app.config.get("BASE_URL") or "").rstrip("/")
    return f"{base}/communications/media/{campaign.id}/{campaign.image_filename or ('campaign_'+str(campaign.id)+'.jpg')}"


def publish_campaign_image(campaign: CommunicationCampaign) -> MediaPublishResult:
    """Publish image to Cloudinary when configured, otherwise use the public DB route.

    Cloudinary is optional. Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and
    CLOUDINARY_API_SECRET to use its CDN. The fallback is still stable because
    the image bytes live in PostgreSQL, not Render's ephemeral filesystem.
    """
    if not campaign.image_data:
        return MediaPublishResult(False, error="Campaign has no image data.")

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    if cloud_name and api_key and api_secret:
        timestamp = int(time.time())
        public_id = f"telesales/whatsapp/campaign_{campaign.id}_{timestamp}"
        signature_base = f"public_id={public_id}&timestamp={timestamp}{api_secret}"
        signature = hashlib.sha1(signature_base.encode("utf-8")).hexdigest()
        files = {
            "file": (
                campaign.image_filename or f"campaign_{campaign.id}.jpg",
                campaign.image_data,
                campaign.image_mimetype or "image/jpeg",
            )
        }
        data = {
            "api_key": api_key,
            "timestamp": str(timestamp),
            "public_id": public_id,
            "signature": signature,
            "overwrite": "true",
        }
        try:
            response = requests.post(
                f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
                data=data,
                files=files,
                timeout=45,
            )
            payload = response.json() if response.content else {}
            if response.status_code < 400 and payload.get("secure_url"):
                return MediaPublishResult(
                    True,
                    url=payload["secure_url"],
                    provider="cloudinary",
                    provider_id=str(payload.get("public_id") or public_id),
                )
            return MediaPublishResult(False, provider="cloudinary", error=payload.get("error", {}).get("message") or response.text)
        except requests.RequestException as exc:
            return MediaPublishResult(False, provider="cloudinary", error=f"Cloudinary upload failed: {exc}")

    url = _public_campaign_image_url(campaign)
    if not url.startswith("https://"):
        return MediaPublishResult(False, error="BASE_URL must be an HTTPS public address before templates can be submitted.")
    return MediaPublishResult(True, url=url, provider="database")


def ensure_media_asset(campaign: CommunicationCampaign) -> MediaPublishResult:
    existing = WhatsAppMediaAsset.query.filter_by(campaign_id=campaign.id, purpose="template_header").order_by(WhatsAppMediaAsset.id.desc()).first()
    if existing and existing.public_url and existing.status == "ready":
        campaign.image_url = existing.public_url
        return MediaPublishResult(True, existing.public_url, existing.storage_provider, existing.provider_asset_id)

    result = publish_campaign_image(campaign)
    asset = existing or WhatsAppMediaAsset(
        campaign_id=campaign.id,
        filename=campaign.image_filename,
        mime_type=campaign.image_mimetype,
        purpose="template_header",
        created_by_id=campaign.created_by_id,
    )
    asset.storage_provider = result.provider
    asset.provider_asset_id = result.provider_id
    asset.public_url = result.url
    asset.status = "ready" if result.ok else "failed"
    asset.last_error = result.error
    if not existing:
        db.session.add(asset)
    if result.ok:
        campaign.image_url = result.url
    db.session.flush()
    checksum = hashlib.sha256(campaign.image_data or b"").hexdigest() if campaign.image_data else None
    last_version = WhatsAppMediaVersion.query.filter_by(media_asset_id=asset.id).order_by(WhatsAppMediaVersion.version_number.desc()).first()
    if campaign.image_data and (not last_version or last_version.checksum != checksum):
        db.session.add(WhatsAppMediaVersion(
            media_asset_id=asset.id,
            version_number=(last_version.version_number + 1) if last_version else 1,
            filename=campaign.image_filename,
            mime_type=campaign.image_mimetype,
            file_data=campaign.image_data,
            public_url=result.url,
            checksum=checksum,
            created_by_id=campaign.created_by_id,
        ))
    db.session.commit()
    return result


def _provider_log(operation, campaign, status, response=None, error=None, started=None):
    duration = int((time.monotonic() - started) * 1000) if started else None
    db.session.add(WhatsAppProviderLog(
        operation=operation,
        campaign_id=campaign.id if campaign else None,
        status=status,
        response_summary=json.dumps(response or {}, default=str)[:20000],
        error=error,
        duration_ms=duration,
    ))


def template_record_for_campaign(campaign: CommunicationCampaign) -> WhatsAppTemplate:
    record = WhatsAppTemplate.query.filter_by(campaign_id=campaign.id).first()
    if record:
        return record
    record = WhatsAppTemplate(
        campaign_id=campaign.id,
        name=campaign.whatsapp_template_name,
        language=campaign.whatsapp_template_language or "en",
        category=campaign.template_category or "MARKETING",
        body_text=campaign.message_body,
        footer_text=campaign.template_footer,
        buttons_json=campaign.template_buttons_json,
        allow_category_change=campaign.template_allow_category_change,
        button_one_text="YES, CALL ME BACK",
        button_two_text="NO THANKS, OPT OUT",
        status=campaign.template_status or "Draft",
        created_by_id=campaign.created_by_id,
    )
    db.session.add(record)
    db.session.flush()
    return record


def submit_campaign_template(campaign: CommunicationCampaign, force: bool = False) -> tuple[bool, str]:
    """Publish media and submit the template. Idempotent unless force=True."""
    template = template_record_for_campaign(campaign)
    if not force and template.status in {"Pending", "Approved"} and template.submitted_at:
        return True, f"Template already {template.status.lower()}."

    media = ensure_media_asset(campaign)
    if not media.ok:
        template.status = "Submission failed"
        template.last_error = media.error
        template.last_checked_at = datetime.utcnow()
        campaign.template_status = template.status
        campaign.template_status_error = media.error
        db.session.commit()
        return False, media.error or "Image publication failed."

    template.header_image_url = media.url
    template.status = "Submitting"
    template.last_error = None
    campaign.template_status = "Submitting"
    campaign.image_url = media.url
    db.session.commit()

    provider_started = time.monotonic()
    try:
        buttons = json.loads(campaign.template_buttons_json or "[]")
    except (TypeError, ValueError):
        buttons = []
    result = create_whatsapp_image_template(
        campaign.whatsapp_template_name,
        campaign.whatsapp_template_language or "en",
        campaign.message_body,
        media.url,
        category=campaign.template_category or "MARKETING",
        footer_text=campaign.template_footer,
        buttons=buttons,
        allow_category_change=campaign.template_allow_category_change,
    )
    now = datetime.utcnow()
    template.last_checked_at = now
    template.last_provider_response = json.dumps(result.response_json or {}, default=str)[:20000]
    _provider_log("submit_template", campaign, "success" if result.ok else "failed", result.response_json, result.error, provider_started)
    if result.ok:
        template.provider_template_id = result.template_id
        template.status = result.status or "Pending"
        template.submitted_at = now
        template.next_check_at = now + timedelta(seconds=60)
        template.last_error = None
        campaign.template_provider_id = result.template_id
        campaign.template_submitted_at = now
        campaign.template_status = template.status
        campaign.template_status_error = None
        campaign.template_checked_at = now
        db.session.add(AgentNotification(
            user_id=campaign.created_by_id,
            title="WhatsApp template submitted",
            message=f"Template {template.name} was sent to Meta and is awaiting approval.",
            notification_type="whatsapp_template_submitted",
            entity_type="campaign",
            entity_id=campaign.id,
        ))
        db.session.commit()
        return True, "Template submitted to Meta."

    template.status = "Submission failed"
    template.last_error = result.error
    template.retry_count = (template.retry_count or 0) + 1
    template.next_check_at = now + timedelta(minutes=min(30, 2 ** min(template.retry_count, 4)))
    campaign.template_status = template.status
    campaign.template_status_error = result.error
    campaign.template_checked_at = now
    db.session.commit()
    queue_provider_job("submit_template", campaign.id, delay_seconds=120, max_attempts=4)
    return False, result.error or "Template submission failed."


def sync_campaign_template(campaign: CommunicationCampaign) -> tuple[bool, str]:
    template = template_record_for_campaign(campaign)
    provider_started = time.monotonic()
    result = get_whatsapp_template_status(template.name, template.language)
    now = datetime.utcnow()
    previous = template.status
    template.last_checked_at = now
    template.next_check_at = now + timedelta(seconds=60 if result.status == "Pending" else 300)
    template.last_provider_response = json.dumps(result.template or {}, default=str)[:20000]
    template.provider_request_id = result.provider_request_id
    template.provider_template_id = result.template_id or template.provider_template_id
    template.quality_rating = result.quality_score
    template.rejection_reason = result.rejection_reason
    template.last_error = result.error
    _provider_log("sync_template", campaign, "success" if result.ok else "failed", result.template, result.error, provider_started)
    if result.ok:
        template.status = result.status
        campaign.template_status = result.status
        campaign.template_checked_at = now
        campaign.template_status_error = result.error
        if result.status == "Approved":
            template.approved_at = template.approved_at or now
            campaign.template_approved_at = campaign.template_approved_at or now
            if previous != "Approved" and not template.approval_notified_at:
                db.session.add(AgentNotification(
                    user_id=campaign.created_by_id,
                    title="WhatsApp template approved",
                    message=f"Template {template.name} for campaign {campaign.name} is approved and ready to send.",
                    notification_type="whatsapp_template_approved",
                    entity_type="campaign",
                    entity_id=campaign.id,
                ))
                template.approval_notified_at = now
                campaign.template_approval_notified_at = now
        elif result.status == "Rejected" and previous != "Rejected":
            db.session.add(AgentNotification(
                user_id=campaign.created_by_id,
                title="WhatsApp template rejected",
                message=f"Template {template.name} was rejected. {result.rejection_reason or result.error or 'Open the campaign for details.'}",
                notification_type="whatsapp_template_rejected",
                entity_type="campaign",
                entity_id=campaign.id,
            ))
    db.session.commit()
    return result.ok, result.error or result.status


def queue_provider_job(job_type: str, campaign_id: int, delay_seconds: int = 0, max_attempts: int = 5) -> WhatsAppProviderJob:
    existing = WhatsAppProviderJob.query.filter_by(job_type=job_type, campaign_id=campaign_id, status="pending").first()
    if existing:
        return existing
    job = WhatsAppProviderJob(
        job_type=job_type,
        campaign_id=campaign_id,
        status="pending",
        run_after=datetime.utcnow() + timedelta(seconds=delay_seconds),
        max_attempts=max_attempts,
    )
    db.session.add(job)
    db.session.commit()
    return job


def process_provider_jobs(limit: int = 20) -> dict[str, int]:
    now = datetime.utcnow()
    jobs = WhatsAppProviderJob.query.filter(
        WhatsAppProviderJob.status == "pending",
        WhatsAppProviderJob.run_after <= now,
    ).order_by(WhatsAppProviderJob.run_after.asc()).limit(limit).all()
    stats = {"processed": 0, "completed": 0, "failed": 0}
    for job in jobs:
        stats["processed"] += 1
        job.status = "running"
        job.started_at = datetime.utcnow()
        db.session.commit()
        try:
            campaign = db.session.get(CommunicationCampaign, job.campaign_id)
            if not campaign:
                job.status = "cancelled"
            elif job.job_type == "submit_template":
                ok, message = submit_campaign_template(campaign, force=True)
                job.status = "completed" if ok else "pending"
                job.last_error = None if ok else message
            elif job.job_type == "sync_template":
                ok, message = sync_campaign_template(campaign)
                job.status = "completed" if ok else "pending"
                job.last_error = None if ok else message
            else:
                job.status = "failed"
                job.last_error = f"Unknown job type: {job.job_type}"
            job.attempt_count = (job.attempt_count or 0) + 1
            if job.status == "pending":
                if job.attempt_count >= job.max_attempts:
                    job.status = "failed"
                else:
                    job.run_after = datetime.utcnow() + timedelta(minutes=min(30, 2 ** job.attempt_count))
            if job.status == "completed":
                stats["completed"] += 1
                job.completed_at = datetime.utcnow()
            elif job.status == "failed":
                stats["failed"] += 1
                job.completed_at = datetime.utcnow()
            db.session.commit()
        except Exception as exc:  # keep queue resilient
            db.session.rollback()
            job = db.session.get(WhatsAppProviderJob, job.id)
            job.attempt_count = (job.attempt_count or 0) + 1
            job.last_error = str(exc)
            job.status = "failed" if job.attempt_count >= job.max_attempts else "pending"
            job.run_after = datetime.utcnow() + timedelta(minutes=min(30, 2 ** job.attempt_count))
            db.session.commit()
            stats["failed"] += int(job.status == "failed")
    return stats


def sync_due_templates(limit: int = 25) -> int:
    due = WhatsAppTemplate.query.filter(
        WhatsAppTemplate.status.in_(["Pending", "Submitting", "Submission failed"]),
        db.or_(WhatsAppTemplate.next_check_at.is_(None), WhatsAppTemplate.next_check_at <= datetime.utcnow()),
    ).order_by(WhatsAppTemplate.next_check_at.asc().nullsfirst()).limit(limit).all()
    count = 0
    for template in due:
        campaign = db.session.get(CommunicationCampaign, template.campaign_id)
        if not campaign:
            continue
        if template.status == "Submission failed":
            submit_campaign_template(campaign, force=True)
        else:
            sync_campaign_template(campaign)
        count += 1
    return count
