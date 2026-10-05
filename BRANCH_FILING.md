# Branch filing

Only wjm@martinsdirect.com can add, edit or delete branch names and filing email addresses at Administration > Branch filing directory. Editable rows populate the application and QA selector. Filing branch is independent of telesales branch.

Before approving an application, select a configured filing branch. Approval attempts to email five signed PDFs: application, welcome pack, POPIA consent, policy disclosure and Annexure J.1. The branch send is tracked independently of client confirmation email. Missing files or failed mail are recorded and can be retried from the application. Automatic repeats are suppressed for an already accepted send to the same branch and recipient. Explicit resends can duplicate copies; interrupted sends require an explicit retry after five minutes.

The application shows destination, last result and full attempt history. Reports > Branch filing report shows approved applications, actual recipient snapshots, results and South African timestamps, with search, result filter and CSV export. Accepted by mail server confirms SMTP acceptance, not branch receipt or completed filing. Historical recipients survive directory edits/deletion. Reselect renamed branches on existing applications.

Existing approved applications are not emailed during deployment. They show Not attempted until staff choose a filing branch and send the pack. Historical office-send audit entries remain on applications and in Reports > Audit reports.

Audit reports, WhatsApp audit/analytics, call analytics and member cover checks are grouped under Reports. Existing access restrictions remain.

Validation uses synthetic documents and mocked SMTP; no real client emails are sent during testing.

The one-time default is Martin's Brokers, Lowhann Barkhuizen, lowhann@martinsdirect.com. Contact name, surname and default selection can be edited. Deleted defaults are not recreated on restart. Newly generated welcome packs include the claim-form step, certified Main Member/deceased/beneficiary IDs (both sides for cards) and DHA-1663, with five sequential steps. Previously signed PDFs are preserved.
