# WhatsApp Image Campaign Build

Added a professional image-template campaign workflow to Communications.

## Included
- Upload JPG, PNG or WEBP advert images.
- Live customer preview showing image, template text and two WhatsApp buttons.
- Approved 360dialog template name and language fields.
- Bulk recipient selection through the existing campaign page.
- Automatic exclusion of opted-out and suppressed contacts.
- Per-recipient quick-reply payloads.
- `YES, CALL ME BACK` creates the existing callback workflow and marks the WhatsApp contact as a callback/hot lead.
- `NO THANKS, OPT OUT` records communication preferences, suppression, and blocks future marketing.
- Existing PostgreSQL databases are patched automatically with the new campaign columns.

## Required 360dialog template
Create and approve a Marketing template with:
1. IMAGE header
2. Body containing one variable for the customer name (`{{1}}`)
3. Quick reply button 1: `YES, CALL ME BACK`
4. Quick reply button 2: `NO THANKS, OPT OUT`

The template name entered in TeleSales must exactly match the approved 360dialog template name.

## Render configuration
Set:
- `WHATSAPP_ENABLED=true`
- `WHATSAPP_PROVIDER=360dialog`
- `D360_API_KEY=<channel API key>`
- `D360_API_BASE_URL=https://waba-v2.360dialog.io`
- `BASE_URL=https://telesales.onrender.com` (or the final custom domain)

For long-term retention of uploaded campaign images, use a Render persistent disk or object storage. The current implementation uses the app's static upload folder.
