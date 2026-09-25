"""Cross-company cover checks for people named on an application.

Only applications with identifiable members and known per-member benefits can
contribute to a total. Imported lead rows have no dependable member benefit
data and must never be mistaken for a negative match.
"""
import json
from decimal import Decimal, InvalidOperation

from app.services.compliance_service import age_from_dob, dob_from_sa_id, is_valid_sa_id, only_digits
from app.services.member_benefits import member_benefit


def cover_limit(age):
    if age is None:
        return None
    if age <= 6:
        return Decimal("10000")
    if age <= 65:
        return Decimal("50000")
    return Decimal("20000")


def _money(value):
    try:
        amount = Decimal(str(value or 0))
        return amount if amount > 0 else Decimal("0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _rows(raw):
    try:
        rows = json.loads(raw or "[]")
        return rows if isinstance(rows, list) else []
    except (TypeError, ValueError):
        return []


def application_members(application):
    """One benefit per covered person, keyed by a validated SA ID when present."""
    product = getattr(application, "product", None)
    main_candidate = only_digits(getattr(application, "id_number", ""))
    main_id = main_candidate if is_valid_sa_id(main_candidate) else ""
    # The issued benefit is stored on the application. Product rules can be
    # edited later and must not silently rewrite cover on an active policy.
    stored_main_cover = _money(getattr(application, "cover_amount", 0))
    main_cover = stored_main_cover or (member_benefit(product, "principal", main_id)["cover"] if product else 0)
    members = [{"role": "Principal member", "name": " ".join(filter(None, [getattr(application, "first_names", ""), getattr(application, "surname", "")])),
                "id_number": main_id, "cover": _money(main_cover), "dob": getattr(application, "date_of_birth", "") or dob_from_sa_id(main_id)}]
    benefits = _rows(getattr(application, "product_dependents_json", None))
    if not any(isinstance(row, dict) and row.get("kind") == "spouse" for row in benefits):
        spouse_name = " ".join(filter(None, [getattr(application, "spouse_first_names", ""), getattr(application, "spouse_surname", "")]))
        if spouse_name or getattr(application, "spouse_id_number", None) or getattr(application, "spouse_date_of_birth", None):
            benefits.append({"kind": "spouse", "full_name": spouse_name,
                             "id_or_dob": getattr(application, "spouse_id_number", "") or getattr(application, "spouse_date_of_birth", "")})
    if not benefits or not any(isinstance(row, dict) and row.get("kind") in {"child", "extended"} for row in benefits):
        for kind, field in (("child", "dependents_json"), ("extended", "extended_family_json")):
            if not any(isinstance(row, dict) and row.get("kind") == kind for row in benefits):
                benefits.extend(dict(row, kind=kind) for row in _rows(getattr(application, field, None)) if isinstance(row, dict))
    for index, row in enumerate(benefits, 1):
        if not isinstance(row, dict) or not any(row.get(key) for key in ("full_name", "id_or_dob", "id_number")):
            continue
        kind = str(row.get("kind") or "member").lower()
        identifier = row.get("id_or_dob") or row.get("id_number") or ""
        identifier_digits = only_digits(identifier)
        if kind == "spouse" and not identifier_digits:
            identifier_digits = only_digits(getattr(application, "spouse_id_number", ""))
        valid_id = identifier_digits if is_valid_sa_id(identifier_digits) else ""
        calculated = member_benefit(product, kind, identifier) if product else {}
        members.append({"role": f"{kind.title()} {index}", "name": str(row.get("full_name") or "").strip(),
                        "id_number": valid_id, "cover": _money(row.get("cover") or calculated.get("cover")),
                        "dob": dob_from_sa_id(valid_id) or row.get("date_of_birth") or identifier})
    return members


def missing_member_ids(application):
    return [member["role"] for member in application_members(application) if not member["id_number"]]


def replace_missing_member_ids(application, submitted):
    """Fill DOB-only members from the unlocked client portal, never replace an existing ID."""
    from app.services.member_benefits import enrich_rows

    rows = _rows(getattr(application, "product_dependents_json", None))
    changed = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not any(row.get(key) for key in ("full_name", "id_or_dob", "id_number")):
            continue
        old = row.get("id_or_dob") or row.get("id_number") or ""
        if is_valid_sa_id(only_digits(old)):
            continue
        candidate = only_digits(submitted.get(f"member_id_{index}", ""))
        if not candidate:
            continue
        if not is_valid_sa_id(candidate):
            raise ValueError(f"Enter a valid South African ID number for {row.get('full_name') or 'member' }.")
        expected_dob = row.get("date_of_birth") or old
        if expected_dob and age_from_dob(expected_dob) is not None:
            from app.services.compliance_service import format_dob
            if format_dob(expected_dob) != format_dob(dob_from_sa_id(candidate)):
                raise ValueError(f"The ID birth date does not match the recorded birth date for {row.get('full_name') or 'member'}.")
        row["id_or_dob"] = candidate
        row["id_number"] = candidate
        row.update(enrich_rows(application.product, row.get("kind") or "member", [row])[0])
        changed.append(row.get("full_name") or f"Member {index + 1}")
    if not changed:
        raise ValueError("Enter at least one outstanding member ID number.")
    application.product_dependents_json = json.dumps(rows)
    application.dependents_json = json.dumps([row for row in rows if row.get("kind") == "child"])
    application.extended_family_json = json.dumps([row for row in rows if row.get("kind") == "extended"])
    spouse = next((row for row in rows if row.get("kind") == "spouse"), None)
    if spouse:
        application.spouse_id_number = spouse.get("id_or_dob") if is_valid_sa_id(only_digits(spouse.get("id_or_dob"))) else ""
        application.spouse_date_of_birth = dob_from_sa_id(application.spouse_id_number) if application.spouse_id_number else spouse.get("date_of_birth")
    return changed


def lock_member_approvals(application):
    """Hold an ID-specific transaction lock until the approval commit."""
    from app import db
    from sqlalchemy import text
    if db.engine.dialect.name != "postgresql":
        return
    ids = sorted({member["id_number"] for member in application_members(application) if member["id_number"]})
    for identifier in ids:
        db.session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identifier, 0))"),
                           {"identifier": identifier})


def coverage_report(application):
    """Evaluate proposed cover against active applications and active office imports."""
    from app.models import ClientApplication, HistoricalMemberCover, LapsedPolicy

    proposed = application_members(application)
    ids = {member["id_number"] for member in proposed if member["id_number"]}
    existing = {identifier: [] for identifier in ids}
    if ids:
        for other in ClientApplication.query.filter(ClientApplication.status.ilike("Active")).yield_per(200):
            if getattr(application, "id", None) and other.id == application.id:
                continue
            for member in application_members(other):
                if member["id_number"] in existing:
                    existing[member["id_number"]].append({"application_ref": other.application_ref,
                        "policy_number": other.policy_number, "status": other.status or "Pending",
                        "company": other.company.company_name if other.company else (other.branch or ""),
                        "product": other.product.product_name if other.product else "",
                        "relationship": member.get("relationship") or "", "cover": member["cover"]})
        for row in HistoricalMemberCover.query.filter(HistoricalMemberCover.id_number.in_(ids),
                                                        HistoricalMemberCover.status.ilike("Active")).all():
            # Office imports are the confirmed issued benefit for a policy.
            # If that policy is also present in the application table, replace
            # the application estimate rather than counting it twice.
            existing[row.id_number] = [item for item in existing[row.id_number]
                                       if item.get("policy_number") != row.policy_number]
            existing[row.id_number].append({"application_ref": row.policy_number,
                "policy_number": row.policy_number, "status": "Active (office import)",
                "company": row.company.company_name if row.company else "",
                "product": row.product_name or "", "relationship": row.relationship or "",
                "cover": _money(row.cover_amount)})
    results = []
    for member in proposed:
        age = age_from_dob(dob_from_sa_id(member["id_number"]) or member["dob"])
        limit = cover_limit(age)
        prior = existing.get(member["id_number"], [])
        existing_cover = sum((row["cover"] for row in prior), Decimal("0"))
        proposed_cover = member["cover"]
        unknown_active_cover = any(row["cover"] <= 0 for row in prior)
        results.append({**member, "age": age, "limit": limit, "existing_cover": existing_cover,
                        "proposed_cover": proposed_cover, "total_cover": existing_cover + proposed_cover,
                        "existing_policies": prior, "unknown_active_cover": unknown_active_cover,
                        "missing_proposed_cover": proposed_cover <= 0,
                        "exceeds": bool(limit is not None and existing_cover + proposed_cover > limit)})
    # Imported lead data may identify a principal member but contains no
    # per-member cover or active-policy indicator; expose this incompleteness.
    imported_matches = (LapsedPolicy.query.filter(LapsedPolicy.id_number.in_(ids)).count() if ids else 0)
    return {"members": results, "missing_ids": missing_member_ids(application),
            "imported_matches_without_cover": imported_matches,
            "blocked": any(row["exceeds"] or row["unknown_active_cover"] or row["missing_proposed_cover"] for row in results)}


def coverage_errors(application, report=None):
    report = report or coverage_report(application)
    errors = [f"{row['role']} cover would be R{row['total_cover']:,.2f} across policies, above the R{row['limit']:,.2f} limit for age {row['age']}."
              for row in report["members"] if row["exceeds"]]
    errors.extend(f"{row['role']} has an active policy with no recorded member cover. Verify that policy before continuing."
                  for row in report["members"] if row["unknown_active_cover"])
    errors.extend(f"{row['role']} has no cover recorded on the selected product."
                  for row in report["members"] if row["missing_proposed_cover"])
    seen = set()
    for row in report["members"]:
        identifier = row["id_number"]
        if not identifier:
            continue
        if identifier in seen:
            errors.append("The same ID number cannot be entered for two covered members on one application.")
            break
        seen.add(identifier)
    return errors
