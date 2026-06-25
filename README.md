# Martin's Funerals Web System Starter

Flask + PostgreSQL + Render starter for:
- Client applications
- Policy product admin and audit logs
- Email signing links
- Electronic signature using drawn signature + typed name + OTP
- Signed application PDF and welcome pack PDF
- Lapsed policy import and recovery call logging
- Default linked roles and permissions

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python seed.py
python run.py
```

Default login:

```text
wjm@martinsdirect.com
Renette7
```

## Render deployment

1. Push this folder to GitHub.
2. In Render, create a Blueprint from `render.yaml`.
3. Add Gmail SMTP app password variables.
4. Set `BASE_URL` to your Render web URL.
5. Run `python seed.py` once in Render Shell.

## Gmail SMTP

Use a Gmail App Password, not your normal Gmail password.

## Important production upgrades

- Move uploaded files to S3/Google Drive when volume grows.
- Add proper SMS OTP provider.
- Add migrations with `flask db init`, `flask db migrate`, `flask db upgrade`.
- Add daily report scheduler worker or Render cron job.


## Script Flow Readability Update
- Sales script now runs in shorter human-speaking steps instead of large paragraphs.
- Each screen shows one conversation point or confirmation.
- Agent text is split into separate speech blocks.
- Progress counter now reflects the expanded guided flow.


## SVG Document Templates

The system now includes the CorelDRAW-exported SVG document templates in `app/static/svg_templates/` and converted PDF backgrounds in `app/static/pdf_templates/`.

Application template selection:
- Family / standard applications: `family_application.svg` -> `application_single_family.pdf`
- Member + Product applications: `member_product_application.svg` -> `application_member_product.pdf`

Terms template selection:
- Family / standard applications: `family_terms.svg` -> `family_terms.pdf`
- Member + Product applications: `member_product_terms.svg` -> `member_product_terms.pdf`

The PDF overlay engine scales legacy field coordinates to A4 so values stay aligned next to headings on the new SVG-derived templates.
