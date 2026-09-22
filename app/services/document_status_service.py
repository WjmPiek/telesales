import json

from app.models import ClientFicaDocument, DocumentSignature

SIGNATURE_DOCUMENTS = [
    ("application", "Application Form"),
    ("popia", "POPIA Consent"),
    ("disclosure", "Policy Disclosure"),
    ("welcome", "Welcome Pack Acknowledgement"),
    ("cdd", "Annexure J.1 - Client Due Diligence"),
]

FICA_LABELS = {
    "id_copy": "South African ID Copy",
    "proof_of_address": "Proof of Address",
    "bank_statement": "Bank Statement / Bank Confirmation",
    "passport": "Passport Copy",
    "permit_visa": "Permit / Visa",
}


STAFF_UPLOAD_LABELS = {**FICA_LABELS, **dict(SIGNATURE_DOCUMENTS), "fic_evidence": "FIC screening screenshot", "other": "Other supporting document"}


def _digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def client_is_sa(application):
    return len(_digits(getattr(application, "id_number", ""))) == 13


def is_debit_order(application):
    return "debit" in str(getattr(application, "payment_method", "") or "").lower()


def required_fica_types(application):
    return [row["key"] for row in required_fica_documents(application)]


def _json_members(raw):
    try:
        rows = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return rows if isinstance(rows, list) else []


def required_fica_documents(application):
    """Return one identity requirement for every person actually on the policy."""
    rows = [{
        "key": "id_copy" if client_is_sa(application) else "passport",
        "label": "Principal Member South African ID Copy" if client_is_sa(application) else "Principal Member Passport Copy",
    }]
    if not client_is_sa(application):
        rows.append({"key": "permit_visa", "label": "Principal Member Permit / Visa"})

    if any((getattr(application, "spouse_first_names", None), getattr(application, "spouse_surname", None),
            getattr(application, "spouse_id_number", None), getattr(application, "spouse_date_of_birth", None))):
        rows.append({"key": "spouse_id_copy", "label": "Spouse ID Copy"})

    rules = getattr(getattr(application, "product", None), "rules", None)
    plan_type = str(getattr(rules, "plan_type", None) or getattr(application, "form_template", None) or "family").lower()
    if plan_type == "member_product":
        member_groups = [("member", "Additional Member", _json_members(getattr(application, "product_dependents_json", None)))]
    else:
        member_groups = [
            ("child", "Child", _json_members(getattr(application, "dependents_json", None))),
            ("extended", "Extended Member", _json_members(getattr(application, "extended_family_json", None))),
        ]
    for prefix, label, members in member_groups:
        for index, member in enumerate(members, 1):
            if not isinstance(member, dict) or not any(member.get(k) for k in ("full_name", "id_or_dob", "relationship")):
                continue
            name = str(member.get("full_name") or "").strip()
            suffix = f" - {name}" if name else ""
            rows.append({"key": f"{prefix}_{index}_id_copy", "label": f"{label} {index} ID Copy{suffix}"})

    rows.append({"key": "proof_of_address", "label": "Proof of Address"})
    return rows


def fica_label(application, key):
    return next((row["label"] for row in required_fica_documents(application) if row["key"] == key),
                FICA_LABELS.get(key, key.replace("_", " ").title()))


def document_summary(application):
    signed_rows = DocumentSignature.query.filter_by(application_id=application.id).all()
    signed_types = {row.document_type: row for row in signed_rows}
    from app.services.signature_fields import signed_documents
    complete_types=signed_documents(application)
    if application.signed_at and application.signed_pdf_path and ("application" in signed_types or "application:principal" in signed_types):complete_types.add("application")
    if "application" in complete_types and "application" not in signed_types:
        signed_types["application"]=signed_types.get("application:principal")
    fica_docs = ClientFicaDocument.query.filter_by(application_id=application.id).order_by(ClientFicaDocument.uploaded_at.desc()).all()

    rows = []
    for key, label in SIGNATURE_DOCUMENTS:
        if key=="cdd" and application.signed_at and key not in signed_types:continue
        manual = next((d for d in fica_docs if d.document_type == key and d.status != 'Replaced'), None)
        is_signed = key in complete_types
        manual_approved = manual and manual.status in {'Reviewed', 'Approved'}
        rows.append({
            "group": "FICA" if manual and not is_signed else "Signature",
            "key": key,
            "label": label,
            "required": True,
            "status": "Signed" if is_signed else ("Approved" if manual_approved else ("Rejected" if manual and manual.status == "Rejected" else "Needs Review" if manual else "Missing")),
            "badge": "success" if is_signed else "danger",
            "signed_at": signed_types[key].signed_at if is_signed else None,
            "document": signed_types.get(key) if is_signed else manual,
            "uploaded_at": manual.uploaded_at if manual else None,
        })

    by_type = {}
    for doc in fica_docs:
        by_type.setdefault(doc.document_type, []).append(doc)

    requirements = required_fica_documents(application)
    for requirement in requirements:
        key, label = requirement["key"], requirement["label"]
        docs = by_type.get(key, [])
        latest = docs[0] if docs else None
        if not latest:
            status, badge = "Missing", "danger"
        elif latest.status in {"Reviewed", "Approved"}:
            status, badge = "Approved", "success"
        elif latest.status == "Rejected":
            status, badge = "Rejected", "danger"
        else:
            status, badge = "Needs Review", "warning"
        rows.append({
            "group": "FICA",
            "key": key,
            "label": label,
            "required": True,
            "status": status,
            "badge": badge,
            "uploaded_at": latest.uploaded_at if latest else None,
            "document": latest,
            "all_documents": docs,
        })

        # The staff FIC screening belongs with the principal identity check in
        # document tracking instead of being a detached application note.
        if key in {"id_copy", "passport"}:
            from app.services.screening_service import latest as latest_screening
            screening = latest_screening(application)
            screening_ok = bool(screening and screening.status in {"Employee checked", "Reviewed"})
            rows.append({
                "group": "Employee FIC",
                "key": "employee_fic",
                "label": "Employee FIC check screenshot",
                "required": True,
                "status": screening.status if screening else "Screenshot required",
                "badge": "success" if screening_ok else ("warning" if screening else "danger"),
                "screening": screening,
                "document": None,
            })

    # Show extra uploaded FICA documents that are not currently required, so nothing is hidden.
    for key, docs in by_type.items():
        if key in {row["key"] for row in requirements} or key in dict(SIGNATURE_DOCUMENTS):
            continue
        latest = docs[0]
        rows.append({
            "group": "FICA",
            "key": key,
            "label": fica_label(application, key) + " (extra)",
            "required": False,
            "status": latest.status or "Received",
            "badge": "secondary",
            "uploaded_at": latest.uploaded_at,
            "document": latest,
            "all_documents": docs,
        })

    missing = [row for row in rows if row["required"] and row["status"] in {"Missing", "Rejected", "Screenshot required"}]
    pending_review = [row for row in rows if row["status"] == "Needs Review"]
    complete = not missing and not pending_review
    return {
        "rows": rows,
        "missing": missing,
        "pending_review": pending_review,
        "complete": complete,
        "completion_percent": round(((len(rows) - len(missing) - len(pending_review)) / len(rows)) * 100) if rows else 0,
        "required_fica_types": required_fica_types(application),
        "fica_labels": FICA_LABELS,
    }
