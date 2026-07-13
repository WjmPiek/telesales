# TeleSales Communications - All Phases Build

This build layers all requested phases into the existing TeleSales workflow.

## Phase 1 - WhatsApp, email, callback and opt-out
- Combined WhatsApp and HTML email campaign sending.
- Secure callback, not-interested and opt-out links.
- Meta WhatsApp webhook verification and response processing.
- Existing callback queue integration.
- POPIA communication preferences and hashed suppression list.

## Phase 2 - Campaign manager
- Campaign creation, recipient selection and search/filtering.
- Duplicate campaigns with optional recipient copying.
- Retry failed sends, archive campaigns and export CSV.
- Per-campaign delivery and response counters.
- Multi-day WhatsApp/email follow-up scheduling.

## Phase 3 - Agent dashboard and notifications
- Assigned-agent callback notifications.
- Unread notification badge in the main navigation.
- Mark-one and mark-all-read actions.
- Direct link into the existing call workflow.

## Phase 4 - Application automation
- Callback records stay linked to `LapsedPolicy`.
- Existing Start Application flow can prefill the online application from the lead.
- Applications are included in campaign conversion reporting.
- Scheduled follow-ups stop automatically after a response.

Run due follow-ups manually:

```powershell
flask process-communication-followups
```

For Render, schedule this command with a Render Cron Job, for example every hour.

## Phase 5 - Reports and analytics
- Campaign analytics dashboard.
- Callback and application conversion rates.
- Delivery, response and opt-out event history.
- CSV campaign export.
- Suppression-list administration view.

## Deployment
1. Deploy the ZIP to Render.
2. Keep `AUTO_CREATE_TABLES=1` for the first deployment so the new helper tables are created.
3. Set `BASE_URL`, mail settings and WhatsApp settings from `.env.example`.
4. Run the application and verify `/communications/`.
5. Configure Meta callback URL as `https://YOUR-DOMAIN/webhooks/whatsapp` only if your deployed route uses that URL. In this build the blueprint URL is `/communications/webhooks/whatsapp`, so use:

   `https://YOUR-DOMAIN/communications/webhooks/whatsapp`

6. Use the same value in Meta and Render for `WHATSAPP_VERIFY_TOKEN`.
7. Create a Render Cron Job for `flask process-communication-followups`.

## Security note
Rotate any WhatsApp access token that appeared in a screenshot or message. Never commit live tokens to GitHub.
