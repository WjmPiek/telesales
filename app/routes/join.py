import secrets
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from app import db
from app.models import (
    ApplicationJourney, AuditLog, CampaignRecipient, ClientApplication,
    CommunicationCampaign, CommunicationEvent, LapsedPolicy, PolicyProduct, User,
)
from app.services.compliance_service import age_from_dob, classify_product_template, dob_from_sa_id, is_valid_sa_id
from app.services.member_benefits import member_limits


join_bp = Blueprint("join", __name__, url_prefix="/join")
COVER_OPTIONS = (10000, 20000, 30000, 40000)


def _principal_cover_errors(product, identifier):
    """Check the same cross-company active-policy ledger as staff QA."""
    from app.services.cover_eligibility import coverage_errors
    proposal = ClientApplication(product=product, id_number=identifier,
                                 cover_amount=product.cover_amount,
                                 date_of_birth=dob_from_sa_id(identifier))
    return coverage_errors(proposal)


def _recipient(token):
    recipient = CampaignRecipient.query.filter_by(secure_token=token).first_or_404()
    if not recipient.policy:
        abort(404)
    return recipient


def _qualification_key(recipient):
    return f"join_qualification_{recipient.id}"


def _product_capacity(product):
    """Return the number of people included in the advertised base product."""
    limits = member_limits(product)
    if limits["plan_type"] == "member_product":
        return 1 + limits["productdep"]
    if limits["plan_type"] == "single":
        return 1
    # Extended-family rows are optional add-ons with separate cover/premium.
    return 1 + limits["spouse"] + limits["child"]


def _product_choices(qualification):
    age = int(qualification["age"])
    cover = Decimal(str(qualification["cover_amount"]))
    total_members = int(qualification["total_members"])
    age_eligible = (
        PolicyProduct.query.filter_by(active=True)
        .filter(db.or_(PolicyProduct.min_age.is_(None), PolicyProduct.min_age <= age))
        .filter(db.or_(PolicyProduct.max_age.is_(None), PolicyProduct.max_age >= age))
        .all()
    )
    # A single applicant must never be offered a family or Member+ package.
    candidates = [p for p in age_eligible if _product_capacity(p) == 1] if total_members == 1 else [
        p for p in age_eligible if _product_capacity(p) >= total_members]
    if qualification.get("id_number"):
        candidates = [p for p in candidates if not _principal_cover_errors(p, qualification["id_number"])]
    # Prefer the package that advertises precisely the requested member count.
    # Only fall back to a larger family package if there is no such package.
    matching_count = [p for p in candidates if _product_capacity(p) == total_members]
    if matching_count:
        candidates = matching_count
    exact = [product for product in candidates
             if Decimal(str(product.cover_amount or 0)) == cover]
    exact.sort(key=lambda product: (product.monthly_premium or 0, product.product_name or ""))
    if exact:
        return exact, False
    alternatives = candidates
    alternatives.sort(key=lambda product: (
        abs(Decimal(str(product.cover_amount or 0)) - cover),
        0 if Decimal(str(product.cover_amount or 0)) <= cover else 1,
        _product_capacity(product) - total_members,
        product.monthly_premium or 0,
        product.product_name or "",
    ))
    return alternatives[:3], True


def _eligible_products(qualification):
    return _product_choices(qualification)[0]


def _resume(application):
    if application.sign_token_used_at or application.sign_token_revoked:
        return redirect(url_for("signing.sign_application", token=application.sign_token))
    from app.routes.signing import _unlocked_key
    session[_unlocked_key(application.id)] = True
    return redirect(url_for("online_application.form", token=application.sign_token))


@join_bp.route("/<token>", methods=["GET", "POST"])
def qualify(token):
    recipient = _recipient(token)
    fixed_product = recipient.campaign.product if recipient.campaign and recipient.campaign.product_id else None
    existing = ClientApplication.query.filter_by(source_campaign_recipient_id=recipient.id).order_by(ClientApplication.id.desc()).first()
    if existing:
        return _resume(existing)

    values = {"id_number": recipient.policy.id_number or "", "total_members": "",
              "cover_amount": str(int(fixed_product.cover_amount)) if fixed_product else ""}
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
        elif fixed_product and (not fixed_product.active or not (fixed_product.min_age is None or fixed_product.min_age <= age_from_dob(dob_from_sa_id(values["id_number"]))) or not (fixed_product.max_age is None or fixed_product.max_age >= age_from_dob(dob_from_sa_id(values["id_number"])))):
            error = "The main member's age does not qualify for this policy. Please ask an agent for assistance."
        elif fixed_product and (cover_errors := _principal_cover_errors(fixed_product, values["id_number"])):
            error = "This product would exceed the cover available for this member: " + "; ".join(cover_errors)
        elif not fixed_product and cover_amount not in COVER_OPTIONS:
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
            if fixed_product:
                return redirect(url_for("join.application", token=token, product_id=fixed_product.id))
            return redirect(url_for("join.products", token=token))
    return render_template("join/qualify.html", recipient=recipient, values=values, cover_options=COVER_OPTIONS, error=error,
                           fixed_product=fixed_product)


@join_bp.route("/<token>/products")
def products(token):
    recipient = _recipient(token)
    qualification = session.get(_qualification_key(recipient))
    if not qualification:
        return redirect(url_for("join.qualify", token=token))
    choices, alternatives = _product_choices(qualification)
    return render_template("join/products.html", recipient=recipient, qualification=qualification,
                           products=choices, alternatives=alternatives, product_capacity=_product_capacity)


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
    cover_errors = _principal_cover_errors(product, qualification["id_number"])
    if cover_errors:
        return render_template("join/products.html", recipient=recipient, qualification=qualification,
                               products=[], alternatives=False, product_capacity=_product_capacity,
                               error="This product is not available: " + "; ".join(cover_errors))

    policy = recipient.policy
    agent = db.session.get(User, policy.assigned_agent_id) if policy.assigned_agent_id else recipient.campaign.created_by
    product_text = f"{product.product_name or ''} {product.plan_name or ''}".lower()
    form_template = "gold_family_fillable" if product.cover_amount == 40000 and "gold" in product_text and "family" in product_text else classify_product_template(product)
    application = ClientApplication(
        application_ref="WEB-" + datetime.utcnow().strftime("%Y%m%d") + "-" + secrets.token_hex(3).upper(),
        product=product,
        agent_id=getattr(agent, "id", None),
        agent_name=getattr(agent, "name", None) or "Self-service",
        branch=policy.branch,
        company_id=policy.company_id,
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


def _campaign_session_key(campaign):
    return f"public_campaign_application_{campaign.id}"


def _normalise_phone(value):
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if digits.startswith("27") and len(digits) == 11:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+27" + digits[1:]
    return None


@join_bp.route("/campaign/<token>", methods=["GET", "POST"])
def campaign_application(token):
    campaign = CommunicationCampaign.query.filter_by(public_application_token=token).first_or_404()
    product = campaign.product
    if not product or not product.active or campaign.deleted_at or campaign.status == "Archived" or (campaign.company and campaign.company.status != "Active"):
        abort(404)
    from app.services.member_benefits import member_limits
    limits = member_limits(product)
    maximum_members = (1 + limits["productdep"] if limits["plan_type"] == "member_product" else
                       1 + limits["spouse"] + limits["child"] + limits["extended"])

    existing_id = session.get(_campaign_session_key(campaign))
    existing = db.session.get(ClientApplication, existing_id) if existing_id else None
    if existing and existing.whatsapp_journey and existing.whatsapp_journey.campaign_id == campaign.id:
        return _resume(existing)

    scan_key = f"campaign_qr_scan_{campaign.id}"
    if request.method == "GET" and not session.get(scan_key):
        campaign.qr_scan_count = (campaign.qr_scan_count or 0) + 1
        db.session.add(CommunicationEvent(campaign_id=campaign.id, event_type="qr_scan", channel="qr",
                                          details="Public product application link opened."))
        db.session.commit()
        session[scan_key] = True

    values = {key: (request.form.get(key) or "").strip() for key in
              ["first_names", "surname", "id_number", "cell_number", "email", "total_members"]}
    error = None
    if request.method == "POST":
        try:
            total_members = int(values["total_members"])
        except ValueError:
            total_members = 0
        phone = _normalise_phone(values["cell_number"])
        dob = dob_from_sa_id(values["id_number"]) if is_valid_sa_id(values["id_number"]) else None
        age = age_from_dob(dob) if dob else None
        if not values["first_names"] or not values["surname"]:
            error = "Enter your first names and surname."
        elif not dob:
            error = "Enter a valid 13-digit South African ID number."
        elif not phone:
            error = "Enter a valid South African mobile number, for example 0821234567."
        elif "@" not in values["email"] or "." not in values["email"].split("@")[-1]:
            error = "Enter a valid email address."
        elif not 1 <= total_members <= maximum_members:
            error = f"Total members must be between 1 and {maximum_members}, including the main member."
        elif (product.min_age is not None and age < product.min_age) or (product.max_age is not None and age > product.max_age):
            error = "The main member's age does not qualify for this policy. Please ask an agent for assistance."
        elif (cover_errors := _principal_cover_errors(product, values["id_number"])):
            error = "This product would exceed the cover available for this member: " + "; ".join(cover_errors)
        else:
            lead = LapsedPolicy(
                member_id=f"QR-{datetime.utcnow():%Y%m%d%H%M%S}-{secrets.token_hex(3)}",
                initials=values["first_names"], surname=values["surname"], id_number=values["id_number"],
                cell_number=phone, email_address=values["email"], branch=campaign.branch,
                company_name=campaign.company.company_name if campaign.company else None,
                franchise=campaign.company.company_name if campaign.company else None,
                company_id=campaign.company_id,
                assigned_agent_id=campaign.created_by_id, recovery_status="Application Started",
                comments=f"Created from QR campaign: {campaign.name}",
            )
            db.session.add(lead)
            db.session.flush()
            product_text = f"{product.product_name or ''} {product.plan_name or ''}".lower()
            form_template = ("gold_family_fillable" if int(product.cover_amount or 0) == 40000 and
                             "gold" in product_text and "family" in product_text else classify_product_template(product))
            agent = db.session.get(User, campaign.created_by_id)
            application = ClientApplication(
                application_ref="QR-" + datetime.utcnow().strftime("%Y%m%d") + "-" + secrets.token_hex(3).upper(),
                product=product, agent_id=campaign.created_by_id,
                agent_name=getattr(agent, "name", None) or "Self-service", branch=campaign.branch,
                company_id=campaign.company_id,
                lapsed_policy_id=lead.id, first_names=values["first_names"], surname=values["surname"],
                id_number="".join(ch for ch in values["id_number"] if ch.isdigit()), date_of_birth=dob,
                cell_number=phone, email=values["email"], document_email=values["email"], status="Draft",
                application_type="New Policy - QR Campaign", payment_method="Cash",
                cover_amount=product.cover_amount, requested_cover=product.cover_amount,
                monthly_premium=product.monthly_premium, total_payment=product.monthly_premium,
                waiting_period=f"{product.waiting_period_months or 0} months", total_members=total_members,
                form_template=form_template, sign_token=secrets.token_urlsafe(32), sign_token_created_at=datetime.utcnow(),
            )
            db.session.add(application)
            db.session.flush()
            db.session.add(ApplicationJourney(application_id=application.id, campaign_id=campaign.id))
            db.session.add(CommunicationEvent(campaign_id=campaign.id, lapsed_policy_id=lead.id,
                                              event_type="application_started", channel="qr",
                                              details=f"{product.product_name}; {total_members} member(s)."))
            db.session.add(AuditLog(action="QR campaign application started", entity_type="ClientApplication",
                                    entity_id=str(application.id), details=f"Started from campaign {campaign.id}: {campaign.name}."))
            db.session.commit()
            session[_campaign_session_key(campaign)] = application.id
            return _resume(application)

    return render_template("join/campaign_application.html", campaign=campaign, product=product, values=values, error=error,
                           maximum_members=maximum_members)
