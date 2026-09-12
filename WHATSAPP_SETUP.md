# WhatsApp setup for Telesales

Website: https://telesales.onrender.com
Repository: https://github.com/WjmPiek/telesales

## Implemented

- Bulk sending uses approved image templates and the existing background scheduler.
- Two quick replies: **Call me back** (index 0), **Delete my number** (index 1).
- Callback creates an agent notification and marks the recovery record Callback; it does not automatically dial a call.
- Delete/STOP clears matching structured phone fields across client, policy, application and recovery records, anonymizes the WhatsApp contact and removes its conversation contents. Matching numbers in selected diagnostic/free-text fields are scrubbed.
- A SHA-256 suppression digest remains to prevent future sending, including reimported numbers. This is a suppression identifier, not a guarantee of irreversible anonymization.
- Webhooks require the Meta app secret/signature and validate button sender ownership. New raw inbound payloads are not retained.
- Template image creation uses Meta's resumable upload handle, fixing the previous image-URL-as-handle error.
- Template language remains exact. Free-text inbox replies require an inbound message within the last 24 hours.

## Render configuration

Update the EXISTING web service. Do not create a second database. Keep the existing DATABASE_URL; rotate the database password shared in chat and update it with the new internal connection string from Render.

Set these in Render Environment, never in GitHub source:

| Variable | Value |
|---|---|
| BASE_URL | https://telesales.onrender.com |
| WHATSAPP_ENABLED | true |
| WHATSAPP_PROVIDER | meta |
| META_APP_ID | 2051811195707571 from screenshot; verify in Meta |
| META_APP_SECRET | App Settings > Basic > App secret |
| META_WABA_ID | Screenshot TEST account is 1329419578845285; replace for production |
| META_PHONE_NUMBER_ID | Copy from Meta API Setup; NOT the displayed telephone number |
| META_ACCESS_TOKEN | Valid system-user token assigned to this app/WABA with whatsapp_business_messaging and whatsapp_business_management permissions |
| META_GRAPH_API_VERSION | v25.0, inherited from project; verify supported in Meta dashboard |
| WHATSAPP_VERIFY_TOKEN | Long random value, identical in Render and Meta webhook settings |
| ENABLE_WHATSAPP_SCHEDULER | 1 |

Start command: `gunicorn --workers 1 --threads 4 --timeout 120 run:app`

Use one service instance with this in-process scheduler. No new database columns are introduced. The existing app creates missing tables at startup. Test on a staging PostgreSQL copy before production deployment.

## Connect Meta

1. Open **Martin's Funerals Sales** in Meta Developers. The screenshots show a TEST sender, not a configured production number.
2. Register the intended production business number and finish billing/account setup in Meta. Initially test with a verified test recipient.
3. Configure callback URL `https://telesales.onrender.com/whatsapp/webhook` and the same WHATSAPP_VERIFY_TOKEN as Render.
4. Subscribe to the WhatsApp `messages` webhook field and ensure the app is subscribed to the correct WABA.
5. Open Communications > WhatsApp Settings in Telesales and run the Meta connection diagnostic.

A successful browser GET only confirms the endpoint exists. It does not establish webhook subscription or delivery.

## Campaign template

Create a new marketing template, for example `martins_callback_delete_v1`.
Upload a JPEG/PNG under 5 MB in the campaign form. The campaign's image URL must be publicly reachable over HTTPS.

Suggested body:

> Hi {{1}}, this is Martin's Northcliff. Would you like a consultant to call you about our funeral cover? Choose Call me back, or Delete my number to remove your number from our contact records.

Use exactly one variable (`{{1}}`, customer name), an IMAGE header and two QUICK_REPLY buttons in this order:

1. Call me back
2. Delete my number

Submit and wait for Meta approval. Existing templates keep their old labels; code changes cannot rename an approved template in Meta. Select recipients who agreed to receive these messages; possession of a number alone does not establish permission.

Click Send to queue the campaign. The scheduler checks periodically, after provider/template jobs. Inspect individual results in the campaign report and WhatsApp inbox.

## First live test

With an expressly approved test recipient, send one message, verify delivery, and press Call me back. Confirm the recovery status and agent notification. Send a separate test campaign and press Delete my number; verify matching phone fields are empty and later sends are suppressed. Then proceed to the intended audience.

## Limits and recovery

- Deletion does not erase numbers inside uploaded files, attachments, images, exports, backups, Meta systems, or every possible free-text representation. Those need separate retention handling. Policy/financial records remain.
- Deletion scans structured records and selected text fields transactionally. Measure on staging for large databases; use a durable erasure worker if this exceeds webhook time limits.
- Provider acceptance is not delivery. Campaign Sent/completed means processing finished; review recipient failures.
- A timeout/crash after Meta accepts a send but before its result commits can leave uncertainty. Reconcile before retrying to avoid duplicates. An interrupted processing campaign needs operator review before requeueing.
- The database supplied was not used for tests. No live client messages have been sent.

## Validation and references

16 automated SQLite tests pass with mocked Meta requests, covering signed webhooks, sender ownership, callback idempotency, STOP/delete, duplicate records, suppression, template upload, language preservation, template-only bulk sending, queued processing and delivery receipts. Production PostgreSQL and real Meta delivery still require verification.

- [Meta template components](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/types/component_object/)
- [Meta resumable upload](https://www.postman.com/meta/whatsapp-business-platform/request/13382743-871ea332-a6a4-4ad0-800a-0be9796b1933)
