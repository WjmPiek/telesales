Only wjm@martinsdirect.com creates and approves Insurance Sales staff accounts.

Public /auth/register GET and POST are closed. Martins SSO accepts an existing active approved account; it cannot create, reactivate, rename, change branch or promote a user. Password login, QR approval, trusted devices, QR desktop completion and session restoration use the same approval rule.

Deployment adds nullable users.owner_approved_at to PostgreSQL before querying users. Existing non-owner accounts start unapproved; do not backfill approval automatically. The owner must review each legitimate account in Administration > Users & employees, choose its role and Active status, and click Approve & save. Account creation by the owner records approval immediately. Suspended accounts remain blocked. The protected owner retains access. No accounts or business history are deleted.

Administration includes Admin Portal and owner-only Audit reports. Audit reports search all historical events by person/email/details, action and South African dates, with pagination and filtered CSV export. New login denials, successful password logins, blocked registrations, SSO denials and account approval/change events are recorded. Passwords and launch tokens are never logged. Older registration events do not contain IP addresses and cannot identify the real submitter retrospectively.

Public client application/signing/upload links remain available; these are client workflows rather than staff login/registration.

Validation: owner access tests cover registration closure, owner creation and approval, direct unauthorized/forged requests, password and session gating, SSO creation/reactivation/promotion prevention, QR/trusted-device gating, audit access/search/export, and exclusion of unapproved accounts from agent reports.
