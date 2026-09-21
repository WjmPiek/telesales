import secrets
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from app import db
from app.models import ApplicationJourney, AuditLog, CampaignRecipient, ClientApplication, PolicyProduct, User
from app.services.compliance_service import age_from_dob, classify_product_template, dob_from_sa_id, is_valid_sa_id


join_bp = Blueprint("join", __name__, url_prefix="/join")
COVER_OPTIONS = (10000, 20000, 30000, 40000)


def _recipient(token):
    recipient = CampaignRecipient.query.filter_by(secure_token=token).first_or_404()
    if not recipient.policy:
        abort(404)
    return recipient


def _qualification_key(recipient):
    return f"join_qualification_{recipient.id}"


def _eligible_products(qualification):
    age = int(qualification["age"])
    cover = Decimal(str(qualification["cover_amount"]))
    return (
        PolicyProduct.query.filter_by(active=True)
        .filter(PolicyProduct.cover_amount == cover)
        .filter(db.or_(PolicyProduct.min_age.is_(None), PolicyProduct.min_age <= age))
        .filter(db.or_(PolicyProduct.max_age.is_(None), PolicyProduct.max_age >= age))
        .order_by(PolicyProduct.monthly_premium.asc(), PolicyProduct.product_name.asc())
        .all()
    )


def _resume(application):
    if application.sign_token_used_at or application.sign_token_revoked:
        return redirect(url_for("signing.sign_application", token=application.sign_token))
    from app.routes.signing import _unlocked_key
    session[_unlocked_key(application.id)] = True
    return redirect(url_for("online_application.form", token=application.sign_token))


@join_bp.route("/<token>", methods=["GET", "POST"])
def qualify(token):
    recipient = _recipient(token)
    existing = ClientApplication.query.filter_by(source_campaign_recipient_id=recipient.id).order_by(ClientApplication.id.desc()).first()
    if existing:
        return _resume(existing)

    values = {"id_number": recipient.policy.id_number or "", "total_members": "", "cover_amount": ""}
    error = None
    if request.method == "POST":
        values = {key: (request.form.get(key) or "").strip() for key in values}
        try:
            total_members = int(values["total_members"])
            cover_amount = int(values["cover_amount"])
        except ValueError:
            total_members = cover_amount = 0
        if not is_valid_sa_id(values["id_number"]):
            error = "Enter a valid 13-digit South African ID number."
        elif not 1 <= total_members <= 11:
            error = "Total members must be between 1 and 11, including the main member."
        elif cover_amount not in COVER_OPTIONS:
            error = "Select one of the available cover amounts."
        else:
            dob = dob_from_sa_id(values["id_number"])
            session[_qualification_key(recipient)] = {
                "id_number": "".join(ch for ch in values["id_number"] if ch.isdigit()),
                "date_of_birth": dob,
                "age": age_from_dob(dob),
                "total_members": total_members,
                "cover_amount": cover_amount,
            }
            return redirect(url_for("join.products", token=token))
    return render_template("join/qualify.html", recipient=recipient, values=values, cover_options=COVER_OPTIONS, error=error)


@join_bp.route("/<token>/products")
def products(token):
    recipient = _recipient(token)
    qualification = session.get(_qualification_key(recipient))
    if not qualification:
        return redirect(url_for("join.qualify", token=token))
    return render_template("join/products.html", recipient=recipient, qualification=qualification, products=_eligible_products(qualification))


@join_bp.route("/<token>/application/<int:product_id>")
def application(token, product_id):
    recipient = _recipient(token)
    existing = ClientApplication.query.filter_by(source_campaign_recipient_id=recipient.id).order_by(ClientApplication.id.desc()).first()
    if existing:
        return _resume(existing)
    qualification = session.get(_qualification_key(recipient))
    if not qualification:
        return redirect(url_for("join.qualify", token=token))
    products = {product.id: product for product in _eligible_products(qualification)}
    product = products.get(product_id)
    if not product:
        abort(400)

    policy = recipient.policy
    agent = db.session.get(User, policy.assigned_agent_id) if policy.assigned_agent_id else recipient.campaign.created_by
    product_text = f"{product.product_name or ''} {product.plan_name or ''}".lower()
    form_template = "gold_family_fillable" if qualification["cover_amount"] == 40000 and "gold" in product_text and "family" in product_text else classify_product_template(product)
    application = ClientApplication(
        application_ref="WEB-" + datetime.utcnow().strftime("%Y%m%d") + "-" + secrets.token_hex(3).upper(),
        product=product,
        agent_id=getattr(agent, "id", None),
        agent_name=getattr(agent, "name", None) or "Self-service",
        branch=policy.branch,
        lapsed_policy_id=policy.id,
        first_names=policy.initials,
        surname=policy.surname,
        id_number=qualification["id_number"],
        date_of_birth=qualification["date_of_birth"],
        cell_number=policy.cell_number,
        email=policy.email_address,
        document_email=policy.email_address,
        address=policy.address,
        residential_address=policy.address,
        status="Draft",
        application_type="New Policy - Self Service",
        payment_method="Cash",
        cover_amount=product.cover_amount,
        monthly_premium=product.monthly_premium,
        total_payment=product.monthly_premium,
        waiting_period=f"{product.waiting_period_months or 0} months",
        total_members=qualification["total_members"],
        requested_cover=Decimal(str(qualification["cover_amount"])),
        source_campaign_recipient_id=recipient.id,
        form_template=form_template,
        sign_token=secrets.token_urlsafe(32),
        sign_token_created_at=datetime.utcnow(),
    )
    db.session.add(application)
    db.session.flush()
    db.session.add(ApplicationJourney(application_id=application.id, campaign_id=recipient.campaign_id))
    recipient.response_type = "Join Now"
    recipient.response_channel = "whatsapp"
    recipient.responded_at = datetime.utcnow()
    db.session.add(AuditLog(
        action="Self-service application started",
        entity_type="ClientApplication",
        entity_id=str(application.id),
        details=f"Started from campaign recipient {recipient.id}; requested {qualification['total_members']} members and R{qualification['cover_amount']} cover.",
    ))
    db.session.commit()
    session.pop(_qualification_key(recipient), None)
    return _resume(application)
