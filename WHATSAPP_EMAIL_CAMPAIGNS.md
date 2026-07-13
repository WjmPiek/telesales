# WhatsApp and Email Campaign Integration

This build adds a Communications module linked to existing `LapsedPolicy` leads, callback queues and online applications.

## Deployment
1. Set `BASE_URL` to the public Render URL.
2. Configure Gmail SMTP variables already used by the system.
3. Configure `WHATSAPP_ENABLED`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and `WHATSAPP_VERIFY_TOKEN`.
4. Deploy. Existing `AUTO_CREATE_TABLES=1` will create the new tables. For controlled production deployments, generate a Flask-Migrate migration before disabling auto-create.
5. Configure the Meta webhook URL as `/communications/webhooks/whatsapp` and use the same verify token.

## Workflow
Managers create a campaign, add existing leads and send WhatsApp/email messages. Secure callback links update the lead to `Callback`, place it in the existing callback worklist, and notify the assigned agent. Opt-outs create suppression hashes and block future campaigns.

## Notes
For production outbound WhatsApp marketing, replace plain text sends with approved Meta template messages. The included webhook supports payloads formatted as `CALLBACK:<secure token>`, `NOTINTERESTED:<secure token>`, `OPTOUT:<secure token>`, or `STOP:<secure token>`.
