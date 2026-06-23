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
