# Automatic 360dialog template status

- Campaign pages now check the configured WhatsApp template status automatically.
- The Send button becomes active only when the provider returns APPROVED/ACTIVE.
- A fresh provider check is repeated server-side immediately before sending.
- Pending, rejected, paused, disabled, deleted, missing, or unreachable templates remain blocked.
- `D360_TEMPLATE_API_URL` can override the default `/v1/configs/templates` endpoint when required.
