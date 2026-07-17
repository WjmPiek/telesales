# Meta WhatsApp Cloud API deployment

The application is fully deployable before Meta approves the business portfolio. WhatsApp screens will load, but provider actions remain unavailable until valid Meta credentials are added.

## Render environment variables

Set these on the Render web service. Never commit real values to Git.

```env
WHATSAPP_ENABLED=true
WHATSAPP_PROVIDER=meta
META_APP_ID=
META_APP_SECRET=
META_WABA_ID=
META_PHONE_NUMBER_ID=
META_ACCESS_TOKEN=
META_GRAPH_API_VERSION=v25.0
WHATSAPP_VERIFY_TOKEN=<long-random-secret>
BASE_URL=https://<service>.onrender.com
ENABLE_WHATSAPP_SCHEDULER=1
```

## Meta webhook

Use:

```text
https://<service>.onrender.com/whatsapp/webhook
```

The verify token must exactly match `WHATSAPP_VERIFY_TOKEN`. Subscribe the WhatsApp Business Account to the `messages` field. Delivery and read receipts are included in message status webhooks.

## First activation checklist

1. Deploy and run database initialization/migrations.
2. Add the Meta environment variables.
3. Verify `/whatsapp/webhook` in Meta.
4. Open **WhatsApp > Settings** and run **Test Meta connection**.
5. Run **Sync templates from Meta**.
6. Send an approved template to one internal test number.
7. Confirm sent, delivered, read, inbound reply, callback and opt-out events.

## Safe pre-approval state

While the Meta business restriction is under review, leave `WHATSAPP_ENABLED=false` in production if employees should not attempt provider actions. The UI, analytics, contacts, campaigns and historical records remain available.
