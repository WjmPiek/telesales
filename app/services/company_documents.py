"""Resolve company-owned client attachments without mixing bank accounts."""
import os
import shutil
import tempfile
from pathlib import Path

from app.models import CompanyDocument, CompanyGroupState


def company_for_application(application):
    if getattr(application, "company_id", None):
        return application.company
    lead = getattr(application, "lapsed_policy", None)
    if lead and lead.company_id:
        return lead.company
    # A legacy application with one unambiguous company in its branch may be
    # recovered. Never guess when two companies share that branch.
    branch = str(getattr(application, "branch", "") or "").strip()
    matches = CompanyGroupState.query.filter_by(branch=branch).limit(2).all() if branch else []
    return matches[0] if len(matches) == 1 else None


def active_company_documents(application, *, include_bank=False):
    company = company_for_application(application)
    if not company:
        return []
    rows = CompanyDocument.query.filter_by(company_id=company.id, active=True).order_by(CompanyDocument.id).all()
    return [row for row in rows if row.category == "additional" or
            (include_bank and row.category == "bank_confirmation")]


def materialise_company_documents(application, *, include_bank=False):
    """Return (paths, temporary folder); caller must remove the folder."""
    company = company_for_application(application)
    if not company:
        return [], None
    rows = active_company_documents(application, include_bank=include_bank)
    if not rows:
        return [], None
    folder = tempfile.mkdtemp(prefix="martins_company_documents_")
    paths = []
    try:
        for row in rows:
            suffix = Path(row.original_filename).suffix.lower()
            if suffix != ".pdf" or not row.file_data.startswith(b"%PDF"):
                raise ValueError("An active company attachment is not a valid PDF.")
            path = os.path.join(folder, f"company_{company.id}_document_{row.id}.pdf")
            Path(path).write_bytes(row.file_data)
            paths.append(path)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return paths, folder
