# Martin's Funerals Web System Starter

Flask + PostgreSQL + Render starter for:
- Client applications
- Policy product admin and audit logs
- Email signing links
- Electronic signature using drawn signature + typed name + OTP
- Signed application PDF and welcome pack PDF
- Lapsed policy import and recovery call logging
- Default linked roles and permissions

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python seed.py
python run.py
```

Default login:

```text
wjm@martinsdirect.com
Renette7
```

## Render deployment

1. Push this folder to GitHub.
2. In Render, create a Blueprint from `render.yaml`.
3. Add Gmail SMTP app password variables.
4. Set `BASE_URL` to your Render web URL.
5. Run `python seed.py` once in Render Shell.

## Gmail SMTP

Use a Gmail App Password, not your normal Gmail password.

## Important production upgrades

- Move uploaded files to S3/Google Drive when volume grows.
- Add proper SMS OTP provider.
- Add migrations with `flask db init`, `flask db migrate`, `flask db upgrade`.
- Add daily report scheduler worker or Render cron job.


## Script Flow Readability Update
- Sales script now runs in shorter human-speaking steps instead of large paragraphs.
- Each screen shows one conversation point or confirmation.
- Agent text is split into separate speech blocks.
- Progress counter now reflects the expanded guided flow.


## SVG Document Templates

The system now includes the CorelDRAW-exported SVG document templates in `app/static/svg_templates/` and converted PDF backgrounds in `app/static/pdf_templates/`.

Application template selection:
- Family / standard applications: `family_application.svg` -> `application_single_family.pdf`
- Member + Product applications: `member_product_application.svg` -> `application_member_product.pdf`

Terms template selection:
- Family / standard applications: `family_terms.svg` -> `family_terms.pdf`
- Member + Product applications: `member_product_terms.svg` -> `member_product_terms.pdf`

The PDF overlay engine scales legacy field coordinates to A4 so values stay aligned next to headings on the new SVG-derived templates.

## Phase 1 telesales workflow

This build adds the first proper call-centre workflow layer:

- Agent dashboard with a **Next Client** button.
- Due-now queue for clients that must be called today.
- Callback tracking through `next_action_date`.
- Required call outcomes.
- Lead status pipeline using `lapsed_policies.recovery_status`.
- Call notes/history from `recovery_call_logs`.

Phase 1 uses existing database columns only. No database migration is required for these changes.

Pipeline statuses used in Phase 1:

```text
New
Imported
Called
No Answer
Callback
Interested
Application Started
Signature Sent
FICA Outstanding
QA Review
Approved
Rejected
Closed
Reinstated
```


## Phase 2 - Callback + Client Timeline

Added on top of Phase 1:

- Callback Worklist at `/recovery/callbacks`
- Overdue, today, upcoming and unscheduled callback sections
- Client Timeline page for every lead
- Timeline records call logs, script sessions, applications, signing links, signatures and FICA uploads
- Callback link added to the top navigation
- Agent dashboard callback card now opens the callback worklist and shows overdue count

No new database columns were added in Phase 2. Existing tables are used so the Render PostgreSQL deployment remains safer.


## Phase 3 - Manager Dashboard

Added manager reporting and worklist screens without adding database columns.

- `/manager` Manager Dashboard
- Agent performance today
- Calls, open leads, callbacks, conversion and pending-work metrics
- Branch filter
- Lead status breakdown
- Pending callbacks, pending signatures, FICA review and QA pending panels

Only Admin, Manager and Branch Manager roles see the Manager link.


## Phase 4 - QA / Compliance

Adds QA/compliance dashboard, application review checklist, approval/rejection decisions, FICA document review, review history, and audit logging. See `PHASE4_NOTES.txt`.


## Phase 5 - Document Tracking

Added document tracking for applications:

- Document dashboard at `/documents`
- Application document status panel
- Required signature document tracking
- Required FICA document tracking
- Missing/rejected document filters
- Pending review filter
- Completion percentage per application
- Staff FICA upload
- Missing-document resend by Email or WhatsApp

No new database columns or tables were added in Phase 5.

## Branch Access Control Update

This package adds branch-level data isolation:
- Agents can only see their assigned branch data.
- Branch Managers/Managers/QA/Compliance can only see their assigned branch data.
- Admin can view all branches or filter by branch.
- Admin can assign branches from **User Approvals / User Branch Access**.

Before going live, make sure every Agent and Branch Manager has a branch value that exactly matches the branch names used in imported leads and applications.


## Phase 15 - Protected User / Agent Management

- Admin and Branch Manager user management added at `/auth/users`.
- Admin can create Branch Managers and Agents and allocate any branch.
- Branch Managers can create/manage Agents only for their own branch.
- Admin users are protected and cannot be edited or deleted.
- User actions are audit logged.
