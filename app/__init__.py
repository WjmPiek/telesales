import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, current_user

from dotenv import load_dotenv

load_dotenv()

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = "auth.login"


def _ensure_lapsed_policy_contact_columns(app):
    """Small Render/PostgreSQL safety patch: add contact/suspense columns when the DB already exists."""
    from sqlalchemy import text
    with app.app_context():
        try:
            if not str(db.engine.url).startswith("postgresql"):
                return
            statements = [
                "ALTER TABLE lapsed_policies ADD COLUMN IF NOT EXISTS callback_at TIMESTAMP",
                "ALTER TABLE telesales_script_sessions ADD COLUMN IF NOT EXISTS script_snapshot_json TEXT",
                "ALTER TABLE client_applications ADD COLUMN IF NOT EXISTS document_email VARCHAR(255)",
                "ALTER TABLE lapsed_policies ADD COLUMN IF NOT EXISTS company_name VARCHAR(160)",
                "ALTER TABLE lapsed_policies ADD COLUMN IF NOT EXISTS id_number VARCHAR(30)",
                "ALTER TABLE lapsed_policies ADD COLUMN IF NOT EXISTS email_address VARCHAR(255)",
                "ALTER TABLE lapsed_policies ADD COLUMN IF NOT EXISTS suspense_reason TEXT",
                "UPDATE lapsed_policies SET company_name = COALESCE(NULLIF(company_name,''), NULLIF(franchise,''), NULLIF(branch,'')) WHERE company_name IS NULL OR company_name = ''",
            ]
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except Exception:
            app.logger.exception("Could not ensure lapsed policy contact/suspense columns")


def _ensure_company_registry(app):
    """Upgrade existing PostgreSQL deployments without altering client records."""
    from sqlalchemy import text
    with app.app_context():
        if not str(db.engine.url).startswith("postgresql"):
            return
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE company_group_states ADD COLUMN IF NOT EXISTS parent_company VARCHAR(160) NOT NULL DEFAULT 'Martin''s Brokers'"))
            conn.execute(text("ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES company_group_states(id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_communication_campaigns_company_id ON communication_campaigns (company_id)"))
            conn.execute(text("ALTER TABLE lapsed_policies ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES company_group_states(id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_lapsed_policies_company_id ON lapsed_policies (company_id)"))
            conn.execute(text("""INSERT INTO company_group_states (company_name, branch, status, parent_company)
                SELECT DISTINCT COALESCE(NULLIF(company_name, ''), NULLIF(franchise, ''), NULLIF(branch, ''), 'Unknown Company'),
                       COALESCE(branch, ''), 'Active', 'Martin''s Brokers'
                FROM lapsed_policies
                WHERE TRUE
                ON CONFLICT (company_name, branch) DO NOTHING"""))
            conn.execute(text("""UPDATE lapsed_policies AS policy
                SET company_id = company.id
                FROM company_group_states AS company
                WHERE policy.company_id IS NULL
                  AND company.company_name = COALESCE(NULLIF(policy.company_name, ''), NULLIF(policy.franchise, ''), NULLIF(policy.branch, ''), 'Unknown Company')
                  AND company.branch = COALESCE(policy.branch, '')"""))


def _ensure_communication_campaign_columns(app):
    """Keep existing Render databases compatible with image-template campaigns."""
    from sqlalchemy import text
    with app.app_context():
        try:
            if not str(db.engine.url).startswith("postgresql"):
                return
            statements = [
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS whatsapp_template_name VARCHAR(160)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS whatsapp_template_language VARCHAR(20) DEFAULT 'en_US'",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_category VARCHAR(40) DEFAULT 'MARKETING'",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_type VARCHAR(40) DEFAULT 'MEDIA_INTERACTIVE'",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_footer VARCHAR(60)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_buttons_json TEXT",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_allow_category_change BOOLEAN DEFAULT TRUE",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS image_filename VARCHAR(255)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS image_url VARCHAR(1000)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS image_data BYTEA",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS image_mimetype VARCHAR(100)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS audience_type VARCHAR(20) DEFAULT 'group'",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_status VARCHAR(30) DEFAULT 'Pending'",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_checked_at TIMESTAMP",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_status_error VARCHAR(1000)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_approved_at TIMESTAMP",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_approved_by_id INTEGER REFERENCES users(id)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_provider_id VARCHAR(160)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_submitted_at TIMESTAMP",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS template_approval_notified_at TIMESTAMP",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMP",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS queue_status VARCHAR(30) DEFAULT 'idle'",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES policy_products(id)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS public_application_token VARCHAR(128)",
                "ALTER TABLE communication_campaigns ADD COLUMN IF NOT EXISTS qr_scan_count INTEGER DEFAULT 0",
                "CREATE INDEX IF NOT EXISTS ix_communication_campaigns_product_id ON communication_campaigns (product_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_communication_campaigns_public_application_token ON communication_campaigns (public_application_token)",
                "UPDATE communication_campaigns SET audience_type = 'group' WHERE audience_type IS NULL OR TRIM(audience_type) = ''",
                "UPDATE communication_campaigns SET template_status = 'Pending' WHERE template_status IS NULL OR TRIM(template_status) = ''",
                "UPDATE communication_campaigns SET queue_status = 'idle' WHERE queue_status IS NULL OR TRIM(queue_status) = ''",
                "UPDATE communication_campaigns SET qr_scan_count = 0 WHERE qr_scan_count IS NULL",
            ]
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except Exception:
            app.logger.exception("Could not ensure communication campaign columns")


def _ensure_whatsapp_template_columns(app):
    """Add Enterprise Communications v3 template-builder fields safely."""
    from sqlalchemy import text
    with app.app_context():
        try:
            if not str(db.engine.url).startswith("postgresql"):
                return
            statements = [
                "ALTER TABLE whatsapp_templates ADD COLUMN IF NOT EXISTS footer_text VARCHAR(60)",
                "ALTER TABLE whatsapp_templates ADD COLUMN IF NOT EXISTS buttons_json TEXT",
                "ALTER TABLE whatsapp_templates ADD COLUMN IF NOT EXISTS components_json TEXT",
                "ALTER TABLE whatsapp_templates ADD COLUMN IF NOT EXISTS allow_category_change BOOLEAN DEFAULT TRUE",
                "UPDATE whatsapp_templates SET allow_category_change = TRUE WHERE allow_category_change IS NULL",
            ]
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except Exception:
            app.logger.exception("Could not ensure WhatsApp v3 template columns")


def _ensure_whatsapp_message_columns(app):
    """Keep delivery receipts linked to campaign recipients on existing databases."""
    from sqlalchemy import text
    with app.app_context():
        try:
            if not str(db.engine.url).startswith("postgresql"):
                return
            statements = [
                "ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS campaign_recipient_id INTEGER REFERENCES campaign_recipients(id)",
                "CREATE INDEX IF NOT EXISTS ix_whatsapp_messages_campaign_recipient_id ON whatsapp_messages (campaign_recipient_id)",
            ]
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except Exception:
            app.logger.exception("Could not ensure WhatsApp campaign message columns")


def _ensure_client_fica_document_columns(app):
    """Render/PostgreSQL safety patch for existing databases.

    Older live databases may have the client_fica_documents table from an
    earlier phase without the newer upload metadata/status columns.  When the
    external client signing page tries to save an ID copy or proof of address,
    PostgreSQL then rejects the INSERT/UPDATE and the upload appears to do
    nothing.  This keeps the schema compatible without wiping any data.
    """
    from sqlalchemy import text
    with app.app_context():
        try:
            if not str(db.engine.url).startswith("postgresql"):
                return
            statements = [
                "ALTER TABLE client_fica_documents ADD COLUMN IF NOT EXISTS document_type VARCHAR(80)",
                "ALTER TABLE client_fica_documents ADD COLUMN IF NOT EXISTS original_filename VARCHAR(255)",
                "ALTER TABLE client_fica_documents ADD COLUMN IF NOT EXISTS file_path VARCHAR(500)",
                "ALTER TABLE client_fica_documents ADD COLUMN IF NOT EXISTS status VARCHAR(40) DEFAULT 'Received'",
                "ALTER TABLE client_fica_documents ADD COLUMN IF NOT EXISTS uploaded_at TIMESTAMP",
                "ALTER TABLE client_fica_documents ADD COLUMN IF NOT EXISTS uploaded_ip VARCHAR(80)",
                "ALTER TABLE client_fica_documents ADD COLUMN IF NOT EXISTS user_agent VARCHAR(500)",
                "UPDATE client_fica_documents SET status = 'Received' WHERE status IS NULL OR TRIM(status) = ''",
                "UPDATE client_fica_documents SET uploaded_at = NOW() WHERE uploaded_at IS NULL",
            ]
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except Exception:
            app.logger.exception("Could not ensure client FICA document columns")


def _ensure_join_now_columns(app):
    """Add the self-service qualification fields without replacing live data."""
    from sqlalchemy import text
    with app.app_context():
        try:
            if not str(db.engine.url).startswith("postgresql"):
                return
            statements = [
                "ALTER TABLE client_applications ADD COLUMN IF NOT EXISTS total_members INTEGER",
                "ALTER TABLE client_applications ADD COLUMN IF NOT EXISTS requested_cover NUMERIC(12,2)",
                "ALTER TABLE client_applications ADD COLUMN IF NOT EXISTS source_campaign_recipient_id INTEGER",
                "CREATE INDEX IF NOT EXISTS ix_client_applications_source_campaign_recipient_id ON client_applications (source_campaign_recipient_id)",
            ]
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except Exception:
            app.logger.exception("Could not ensure Join Now application columns")


def _ensure_policy_product_rule_columns(app):
    """Add editable product member configuration to existing live databases."""
    from sqlalchemy import text
    with app.app_context():
        try:
            if not str(db.engine.url).startswith("postgresql"):
                return
            statements = [
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS plan_type VARCHAR(40)",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS spouse_slots INTEGER DEFAULT 1",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS child_slots INTEGER DEFAULT 6",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS extended_slots INTEGER DEFAULT 6",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS extra_member_slots INTEGER DEFAULT 13",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS spouse_min_age INTEGER",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS spouse_max_age INTEGER",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS child_min_age INTEGER DEFAULT 0",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS child_max_age INTEGER DEFAULT 21",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS extended_min_age INTEGER DEFAULT 0",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS extended_max_age INTEGER DEFAULT 100",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS extra_member_min_age INTEGER DEFAULT 0",
                "ALTER TABLE policy_product_rules ADD COLUMN IF NOT EXISTS extra_member_max_age INTEGER DEFAULT 70",
                "UPDATE policy_product_rules r SET plan_type = 'member_product' FROM policy_products p WHERE r.product_id = p.id AND (r.plan_type IS NULL OR TRIM(r.plan_type) = '') AND (LOWER(p.product_name) LIKE '%member +%' OR LOWER(p.product_name) LIKE '%member+%' OR LOWER(p.plan_name) LIKE '%member +%' OR LOWER(p.plan_name) LIKE '%member+%')",
                "UPDATE policy_product_rules SET plan_type = 'family' WHERE plan_type IS NULL OR TRIM(plan_type) = ''",
                "ALTER TABLE policy_product_rules ALTER COLUMN plan_type SET DEFAULT 'family'",
                "UPDATE policy_product_rules SET spouse_slots = 1 WHERE spouse_slots IS NULL",
                "UPDATE policy_product_rules SET child_slots = 6 WHERE child_slots IS NULL",
                "UPDATE policy_product_rules SET extended_slots = 6 WHERE extended_slots IS NULL",
                "UPDATE policy_product_rules SET extra_member_slots = 13 WHERE extra_member_slots IS NULL",
                "UPDATE policy_product_rules r SET spouse_min_age = COALESCE(p.min_age, 18), spouse_max_age = COALESCE(p.max_age, 70) FROM policy_products p WHERE r.product_id = p.id AND (r.spouse_min_age IS NULL OR r.spouse_max_age IS NULL)",
                "ALTER TABLE policy_product_rules ALTER COLUMN spouse_min_age SET DEFAULT 18",
                "ALTER TABLE policy_product_rules ALTER COLUMN spouse_max_age SET DEFAULT 70",
                "UPDATE policy_product_rules SET child_min_age = 0 WHERE child_min_age IS NULL",
                "UPDATE policy_product_rules SET child_max_age = 21 WHERE child_max_age IS NULL",
                "UPDATE policy_product_rules SET extended_min_age = 0 WHERE extended_min_age IS NULL",
                "UPDATE policy_product_rules SET extended_max_age = 100 WHERE extended_max_age IS NULL",
                "UPDATE policy_product_rules SET extra_member_min_age = 0 WHERE extra_member_min_age IS NULL",
                "UPDATE policy_product_rules SET extra_member_max_age = 70 WHERE extra_member_max_age IS NULL",
            ]
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except Exception:
            app.logger.exception("Could not ensure policy product member configuration columns")


def _ensure_bank_confirmation_letters_table(app):
    """Preserve versioned bank letters in PostgreSQL instead of Render's ephemeral disk."""
    from sqlalchemy import text
    with app.app_context():
        try:
            if not str(db.engine.url).startswith("postgresql"):
                return
            statements = [
                "CREATE TABLE IF NOT EXISTS bank_confirmation_letters (id SERIAL PRIMARY KEY, original_filename VARCHAR(255) NOT NULL, file_data BYTEA NOT NULL, file_size INTEGER NOT NULL DEFAULT 0, checksum_sha256 VARCHAR(64) NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE, uploaded_at TIMESTAMP NOT NULL DEFAULT NOW(), uploaded_by_id INTEGER NOT NULL REFERENCES users(id))",
                "CREATE INDEX IF NOT EXISTS ix_bank_confirmation_letters_checksum_sha256 ON bank_confirmation_letters (checksum_sha256)",
                "CREATE INDEX IF NOT EXISTS ix_bank_confirmation_letters_active ON bank_confirmation_letters (active)",
            ]
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except Exception:
            app.logger.exception("Could not ensure bank confirmation letter storage")


def create_app():
    app = Flask(__name__)
    from app.services.branding import display_brand
    app.jinja_env.finalize = display_brand
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key and os.getenv("FLASK_ENV") == "production":
        raise RuntimeError("SECRET_KEY must be set in production")
    app.config["SECRET_KEY"] = secret_key or "dev-secret-change-me"
    db_url = os.getenv("DATABASE_URL", "sqlite:///dev.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # Render/Postgres can close idle SSL connections. These options force
    # SQLAlchemy to test/recycle connections instead of reusing dead ones.
    if db_url.startswith("postgresql"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True,
            "pool_recycle": 280,
            "pool_timeout": 30,
            "pool_size": 5,
            "max_overflow": 2,
        }
    upload_folder = os.getenv("UPLOAD_FOLDER")
    if upload_folder:
        upload_folder = os.path.abspath(upload_folder)
    else:
        upload_folder = os.path.join(app.instance_path, "uploads")
    app.config["UPLOAD_FOLDER"] = upload_folder
    app.config["BASE_URL"] = os.getenv("BASE_URL", "http://localhost:5000")
    app.config["WHATSAPP_VERIFY_TOKEN"] = os.getenv("WHATSAPP_VERIFY_TOKEN")

    try:
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    except Exception:
        # Do not prevent the app from starting if a Render upload path is not mounted.
        # The signing upload route will fall back to instance/uploads or /tmp/telesales_uploads.
        app.logger.warning("Configured UPLOAD_FOLDER is not writable at startup: %s", app.config["UPLOAD_FOLDER"])

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from app.models import User

    # Auto-create missing tables for small Render deployments. This keeps new
    # helper tables, such as QR login tokens, from breaking existing databases.
    # Proper Flask migrations can still be added later.
    if os.getenv("AUTO_CREATE_TABLES", "1") == "1":
        with app.app_context():
            db.create_all()
            try:
                _ensure_lapsed_policy_contact_columns(app)
            except Exception:
                pass
            try:
                _ensure_client_fica_document_columns(app)
            except Exception:
                pass
            try:
                _ensure_join_now_columns(app)
            except Exception:
                pass
            try:
                _ensure_policy_product_rule_columns(app)
                _ensure_bank_confirmation_letters_table(app)
            except Exception:
                pass
            try:
                _ensure_communication_campaign_columns(app)
                _ensure_whatsapp_template_columns(app)
                _ensure_whatsapp_message_columns(app)
            except Exception:
                pass
            try:
                from app.models import Role, User
                super_role = Role.query.filter_by(name="Super Admin").first()
                if not super_role:
                    super_role = Role(name="Super Admin", description="Protected Super Admin account")
                    db.session.add(super_role)
                    db.session.flush()
                super_user = User.query.filter(db.func.lower(User.email) == "wjm@martinsdirect.com").first()
                if super_user:
                    super_user.role = super_role
                    super_user.active = True
                    db.session.commit()
                else:
                    db.session.commit()
            except Exception:
                db.session.rollback()

    try:
        _ensure_lapsed_policy_contact_columns(app)
    except Exception:
        pass
    try:
        _ensure_client_fica_document_columns(app)
    except Exception:
        pass
    try:
        _ensure_join_now_columns(app)
    except Exception:
        pass
    try:
        _ensure_policy_product_rule_columns(app)
        _ensure_bank_confirmation_letters_table(app)
    except Exception:
        pass
    try:
        _ensure_communication_campaign_columns(app)
        _ensure_whatsapp_template_columns(app)
        _ensure_whatsapp_message_columns(app)
    except Exception:
        pass

    # Keep this small, new scheduling table available even when general
    # AUTO_CREATE_TABLES is disabled on a production deployment.
    with app.app_context():
        from app.models import SupportingDocumentReminder, CompanyGroupState
        SupportingDocumentReminder.__table__.create(db.engine, checkfirst=True)
        CompanyGroupState.__table__.create(db.engine, checkfirst=True)
        from app.models import company_agent_assignments
        company_agent_assignments.create(db.engine, checkfirst=True)
        try:
            _ensure_company_registry(app)
        except Exception:
            app.logger.exception("Company registry upgrade failed")
        try:
            from app.services.brokers_branch import reconcile_brokers_branch
            reconcile_brokers_branch()
        except Exception:
            db.session.rollback()
            app.logger.exception("Protected Brokers branch correction failed")
        try:
            from app.services.user_delete_protection import ensure_scoped_user_delete_guard
            if ensure_scoped_user_delete_guard():
                app.logger.info("Installed scoped user-deletion guard; TRUNCATE protection remains active")
        except Exception:
            db.session.rollback()
            app.logger.exception("Scoped user-deletion guard installation failed")

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(User, int(user_id))
        return user if user and user.active else None


    @app.teardown_request
    def shutdown_session(exception=None):
        if exception is not None:
            db.session.rollback()
        db.session.remove()

    @app.cli.command("process-whatsapp-jobs")
    def process_whatsapp_jobs_command():
        """Process queued Meta template submission/status jobs."""
        from app.services.whatsapp_enterprise import process_provider_jobs, sync_due_templates
        stats = process_provider_jobs(limit=50)
        synced = sync_due_templates(limit=50)
        print({"jobs": stats, "templates_synced": synced})

    @app.cli.command("process-document-reminders")
    def process_document_reminders_command():
        from app.services.document_reminders import process_document_reminders
        print(process_document_reminders())

    # One-worker Render deployments can safely run this lightweight scheduler.
    # Set ENABLE_WHATSAPP_SCHEDULER=0 if a dedicated worker/cron service is used.
    if os.getenv("ENABLE_WHATSAPP_SCHEDULER", "1").lower() in {"1", "true", "yes"}:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
            def _whatsapp_tick():
                with app.app_context():
                    try:
                        from app.services.whatsapp_enterprise import process_provider_jobs, sync_due_templates
                        process_provider_jobs(limit=20)
                        sync_due_templates(limit=25)
                        from app.services.whatsapp_campaign_engine import process_scheduled_campaigns
                        process_scheduled_campaigns(limit=10)
                    except Exception:
                        app.logger.exception("Automatic WhatsApp template monitor failed")
                    try:
                        from app.services.document_reminders import process_document_reminders
                        process_document_reminders(limit=25)
                    except Exception:
                        app.logger.exception("Automatic supporting-document reminders failed")
            scheduler.add_job(_whatsapp_tick, "interval", seconds=60, id="whatsapp_provider_monitor", replace_existing=True, max_instances=1, coalesce=True)
            scheduler.start()
            app.extensions["whatsapp_scheduler"] = scheduler
        except Exception:
            app.logger.exception("Could not start WhatsApp background scheduler")

    from app.routes.auth import auth_bp
    from app.routes.main import main_bp
    from app.routes.applications import applications_bp
    from app.routes.signing import signing_bp
    from app.routes.policies import policies_bp
    from app.routes.recovery import recovery_bp
    from app.routes.qa import qa_bp
    from app.routes.documents import documents_bp
    from app.routes.advanced import advanced_bp
    from app.routes.security_center import security_center_bp
    from app.routes.settings import settings_bp
    from app.routes.reports import reports_bp
    from app.routes.role_portals import role_portals_bp
    from app.routes.allocation import allocation_bp
    from app.routes.wallboard import wallboard_bp
    from app.routes.targets import targets_bp
    from app.routes.analytics import analytics_bp
    from app.routes.communications import communications_bp
    from app.routes.whatsapp import whatsapp_bp

    from app.routes.client_files import client_files_bp
    app.register_blueprint(client_files_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(applications_bp)
    app.register_blueprint(signing_bp)
    from app.routes.online_application import online_bp
    app.register_blueprint(online_bp)
    from app.routes.join import join_bp
    app.register_blueprint(join_bp)
    app.register_blueprint(policies_bp)
    app.register_blueprint(recovery_bp)
    app.register_blueprint(qa_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(advanced_bp)
    app.register_blueprint(security_center_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(role_portals_bp)
    app.register_blueprint(allocation_bp)
    app.register_blueprint(wallboard_bp)
    app.register_blueprint(targets_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(communications_bp)
    app.register_blueprint(whatsapp_bp)

    # Keep /webhook as a backward-compatible alias. Meta should use /whatsapp/webhook.
    from app.routes.whatsapp import webhook as whatsapp_webhook
    app.add_url_rule(
        "/webhook",
        endpoint="whatsapp_webhook_root",
        view_func=whatsapp_webhook,
        methods=["GET", "POST"],
    )

    @app.context_processor
    def communication_badges():
        try:
            if not getattr(current_user, "is_authenticated", False):
                return {"unread_notification_count": 0}
            from app.models import AgentNotification
            count = AgentNotification.query.filter_by(user_id=current_user.id, is_read=False).count()
            return {"unread_notification_count": count}
        except Exception:
            return {"unread_notification_count": 0}

    @app.cli.command("process-communication-followups")
    def process_communication_followups():
        """Send due WhatsApp/email follow-ups that have no client response."""
        from datetime import datetime
        from app.models import CommunicationFollowUp
        from app.routes.communications import _send_to_recipient
        jobs = CommunicationFollowUp.query.filter(
            CommunicationFollowUp.status == "Pending",
            CommunicationFollowUp.due_at <= datetime.utcnow(),
        ).order_by(CommunicationFollowUp.due_at.asc()).limit(500).all()
        sent = failed = skipped = 0
        for job in jobs:
            if job.channel != "whatsapp":
                job.status = "Skipped"
                job.processed_at = datetime.utcnow()
                job.last_error = "Campaigns support WhatsApp only"
                skipped += 1
                continue
            recipient = job.recipient
            if recipient.response_type:
                job.status = "Skipped"; job.processed_at = datetime.utcnow(); skipped += 1
                continue
            ok, error = _send_to_recipient(job.campaign, recipient, job.channel)
            job.attempt_count += 1; job.processed_at = datetime.utcnow()
            job.status = "Sent" if ok else "Failed"; job.last_error = error
            sent += int(ok); failed += int(not ok)
        db.session.commit()
        print(f"Processed {len(jobs)} follow-ups: {sent} sent, {failed} failed, {skipped} skipped")

    return app
