# Client application flow

Email uses authenticated SMTP over SSL or STARTTLS. Configure SMTP_HOST, SMTP_PORT, SMTP_SECURITY (ssl or starttls), SMTP_USERNAME and SMTP_PASSWORD. Set MAIL_FROM, MAIL_REPLY_TO and MAIL_DOCUMENTS_TO to the authorised company mailbox. Legacy Gmail credentials are used only for smtp.gmail.com when explicit SMTP credentials are absent. No plaintext SMTP is supported.

The completed script prepares a secure signing link. Clients unlock the page with their main member ID, upload FICA documents, sign each required document and submit. Signed documents are stored in a private per-member/per-application folder and in client_stored_files in the application database. The database is the durable copy; missing local files are restored on access. Keep database backups and monitor database storage as documents accumulate. Uploaded documents still require staff review. This flow uses ID-number unlock, not OTP verification.

On submission, the client receives a receipt and MAIL_DOCUMENTS_TO receives a notification linking to the authenticated client file. Replies go to MAIL_REPLY_TO. Email attachments replied directly to the mailbox are not automatically imported: staff must upload them through the application document panel. Online signing/uploading saves them directly.

Clients > Find client / client file searches main member ID and groups accessible applications, calls, scripts, WhatsApp messages and documents into tabs. WhatsApp matching uses the saved phone number; shared numbers can contain household conversations. Branch and agent scope applies before records are displayed.

Storage is additive: existing stored paths remain readable. No users, system settings, SMTP credentials or client records are deleted by this update.

Validation: tests/test_application_flow.py covers email failure status, SMTP sender/reply-to, legacy credential isolation, secure signing access, upload, signatures, final submission, document restoration, ID search and branch isolation.
