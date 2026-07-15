# TeleSales Enterprise WhatsApp v2

This build moves day-to-day WhatsApp operations into TeleSales while 360dialog remains the background provider.

## Included

- Automatic campaign image publishing.
- Optional Cloudinary CDN storage; PostgreSQL-backed public image fallback.
- Automatic template submission to 360dialog/Meta when a campaign is created.
- No Render self-request validation deadlock.
- Persistent template registry and provider diagnostics.
- Automatic 60-second approval monitoring.
- Approved/rejected in-system notifications.
- Automatic retry queue with exponential backoff.
- WhatsApp Enterprise dashboard.
- WhatsApp Template Library.
- Provider job queue visibility.
- Individual and group campaign support retained.
- Callback and opt-out processing retained.
- Campaign deletion updated for new enterprise records.
- CLI command: `flask process-whatsapp-jobs`.

## Required Render environment

```
BASE_URL=https://telesales.onrender.com
WHATSAPP_ENABLED=true
WHATSAPP_PROVIDER=360dialog
D360_API_KEY=...
D360_API_BASE_URL=https://waba-v2.360dialog.io
ENABLE_WHATSAPP_SCHEDULER=1
```

## Optional Cloudinary CDN

```
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
```

When Cloudinary is not configured, campaign images remain stored in PostgreSQL and are served through the public campaign image endpoint.
