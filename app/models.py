from datetime import datetime, date
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db

role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.Integer, db.ForeignKey("roles.id"), primary_key=True),
    db.Column("permission_id", db.Integer, db.ForeignKey("permissions.id"), primary_key=True),
)

class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(255))
    permissions = db.relationship("Permission", secondary=role_permissions, backref="roles")

class Permission(db.Model):
    __tablename__ = "permissions"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.String(255))

class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    branch = db.Column(db.String(120))
    active = db.Column(db.Boolean, default=True)

    # Full application form fields
    agent_name = db.Column(db.String(150))
    agent_code = db.Column(db.String(80))
    title = db.Column(db.String(30))
    date_of_birth = db.Column(db.String(30))
    spouse_title = db.Column(db.String(30))
    spouse_first_names = db.Column(db.String(150))
    spouse_surname = db.Column(db.String(150))
    spouse_id_number = db.Column(db.String(30))
    spouse_date_of_birth = db.Column(db.String(30))
    residential_address = db.Column(db.Text)
    residential_postal_code = db.Column(db.String(20))
    postal_address = db.Column(db.Text)
    postal_code = db.Column(db.String(20))
    home_tel = db.Column(db.String(50))
    work_tel = db.Column(db.String(50))
    plan_choice = db.Column(db.String(50))
    extended_premium = db.Column(db.Numeric(12,2), default=0)
    total_payment = db.Column(db.Numeric(12,2), default=0)

    dependents_json = db.Column(db.Text)       # children/family dependents as JSON
    extended_family_json = db.Column(db.Text)  # extended family rows as JSON
    product_dependents_json = db.Column(db.Text)

    beneficiary_full_names = db.Column(db.String(200))
    beneficiary_title = db.Column(db.String(30))
    beneficiary_id_number = db.Column(db.String(30))
    beneficiary_date_of_birth = db.Column(db.String(30))
    beneficiary_relationship = db.Column(db.String(80))

    payment_method = db.Column(db.String(80))
    first_deduction_date = db.Column(db.String(30))
    debit_day = db.Column(db.String(20))
    bank_name = db.Column(db.String(120))
    branch_name = db.Column(db.String(120))
    branch_code = db.Column(db.String(50))
    bank_town = db.Column(db.String(120))
    account_number = db.Column(db.String(80))
    account_type = db.Column(db.String(80))
    account_holder = db.Column(db.String(180))

    employer = db.Column(db.String(180))
    salary = db.Column(db.Numeric(12,2), default=0)
    persal_no = db.Column(db.String(80))
    paypoint = db.Column(db.String(120))
    payroll_premium = db.Column(db.Numeric(12,2), default=0)
    personal_holder = db.Column(db.String(180))
    department_code = db.Column(db.String(80))

    form_template = db.Column(db.String(80))   # single_family or member_product

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    role = db.relationship("Role")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    def has_permission(self, code):
        if not self.role:
            return False
        return any(p.code == code for p in self.role.permissions)

class PolicyProduct(db.Model):
    __tablename__ = "policy_products"
    id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(150), nullable=False)
    plan_name = db.Column(db.String(150), nullable=False)
    cover_amount = db.Column(db.Numeric(12,2), default=0)
    monthly_premium = db.Column(db.Numeric(12,2), default=0)
    waiting_period_months = db.Column(db.Integer, default=0)
    min_age = db.Column(db.Integer)
    max_age = db.Column(db.Integer)
    active = db.Column(db.Boolean, default=True)

    # Full application form fields
    agent_name = db.Column(db.String(150))
    agent_code = db.Column(db.String(80))
    title = db.Column(db.String(30))
    date_of_birth = db.Column(db.String(30))
    spouse_title = db.Column(db.String(30))
    spouse_first_names = db.Column(db.String(150))
    spouse_surname = db.Column(db.String(150))
    spouse_id_number = db.Column(db.String(30))
    spouse_date_of_birth = db.Column(db.String(30))
    residential_address = db.Column(db.Text)
    residential_postal_code = db.Column(db.String(20))
    postal_address = db.Column(db.Text)
    postal_code = db.Column(db.String(20))
    home_tel = db.Column(db.String(50))
    work_tel = db.Column(db.String(50))
    plan_choice = db.Column(db.String(50))
    extended_premium = db.Column(db.Numeric(12,2), default=0)
    total_payment = db.Column(db.Numeric(12,2), default=0)

    dependents_json = db.Column(db.Text)       # children/family dependents as JSON
    extended_family_json = db.Column(db.Text)  # extended family rows as JSON
    product_dependents_json = db.Column(db.Text)

    beneficiary_full_names = db.Column(db.String(200))
    beneficiary_title = db.Column(db.String(30))
    beneficiary_id_number = db.Column(db.String(30))
    beneficiary_date_of_birth = db.Column(db.String(30))
    beneficiary_relationship = db.Column(db.String(80))

    payment_method = db.Column(db.String(80))
    first_deduction_date = db.Column(db.String(30))
    debit_day = db.Column(db.String(20))
    bank_name = db.Column(db.String(120))
    branch_name = db.Column(db.String(120))
    branch_code = db.Column(db.String(50))
    bank_town = db.Column(db.String(120))
    account_number = db.Column(db.String(80))
    account_type = db.Column(db.String(80))
    account_holder = db.Column(db.String(180))

    employer = db.Column(db.String(180))
    salary = db.Column(db.Numeric(12,2), default=0)
    persal_no = db.Column(db.String(80))
    paypoint = db.Column(db.String(120))
    payroll_premium = db.Column(db.Numeric(12,2), default=0)
    personal_holder = db.Column(db.String(180))
    department_code = db.Column(db.String(80))

    form_template = db.Column(db.String(80))   # single_family or member_product

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class PolicyChangeLog(db.Model):
    __tablename__ = "policy_change_logs"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("policy_products.id"))
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    field_name = db.Column(db.String(120))
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    reason = db.Column(db.Text)
    changed_at = db.Column(db.DateTime, default=datetime.utcnow)


class PolicyProductRule(db.Model):
    __tablename__ = "policy_product_rules"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("policy_products.id"), nullable=False, unique=True)
    qty_cover = db.Column(db.String(80))
    workers_policy_premium_per_1000 = db.Column(db.Numeric(12,2), default=0)
    suicide_waiting_period = db.Column(db.String(120))
    age_limit = db.Column(db.String(120))
    joining_fee = db.Column(db.Numeric(12,2), default=0)
    workers_policy_cover = db.Column(db.Numeric(12,2), default=0)
    main_member_cover = db.Column(db.Numeric(12,2), default=0)
    spouse_cover = db.Column(db.Numeric(12,2), default=0)
    extended_cover = db.Column(db.Numeric(12,2), default=0)
    member_0_5_product_only = db.Column(db.Numeric(12,2), default=0)
    member_6_70_product_only = db.Column(db.Numeric(12,2), default=0)
    stillborn_cover = db.Column(db.Numeric(12,2), default=0)
    family_0_11 = db.Column(db.Numeric(12,2), default=0)
    family_1_5 = db.Column(db.Numeric(12,2), default=0)
    family_6_13 = db.Column(db.Numeric(12,2), default=0)
    family_14_21 = db.Column(db.Numeric(12,2), default=0)
    reinstatement_rules = db.Column(db.Text)
    email_rule = db.Column(db.String(255))
    sms_whatsapp_rule = db.Column(db.String(255))
    require_draw_signature = db.Column(db.Boolean, default=True)
    require_typed_signature = db.Column(db.Boolean, default=True)
    require_otp = db.Column(db.Boolean, default=True)
    document_storage = db.Column(db.String(255))
    source_file = db.Column(db.String(255))
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)
    product = db.relationship("PolicyProduct", backref=db.backref("rules", uselist=False))

class ClientApplication(db.Model):
    __tablename__ = "client_applications"
    id = db.Column(db.Integer, primary_key=True)
    application_ref = db.Column(db.String(50), unique=True, nullable=False)
    policy_number = db.Column(db.String(80))
    product_id = db.Column(db.Integer, db.ForeignKey("policy_products.id"))
    branch = db.Column(db.String(120))
    agent_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    status = db.Column(db.String(50), default="Draft")
    application_type = db.Column(db.String(50), default="New Policy")  # New Policy, Reinstatement, Lapsed New Policy
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"))
    original_policy_number = db.Column(db.String(80))
    joining_fee = db.Column(db.Numeric(12,2), default=0)
    joining_fee_waived = db.Column(db.Boolean, default=False)
    joining_fee_waiver_reason = db.Column(db.String(255))

    first_names = db.Column(db.String(150))
    surname = db.Column(db.String(150))
    id_number = db.Column(db.String(30))
    cell_number = db.Column(db.String(30))
    email = db.Column(db.String(255))
    address = db.Column(db.Text)
    inception_date = db.Column(db.Date)
    cover_amount = db.Column(db.Numeric(12,2), default=0)
    monthly_premium = db.Column(db.Numeric(12,2), default=0)
    waiting_period = db.Column(db.String(120))


    # Full application form fields
    agent_name = db.Column(db.String(150))
    agent_code = db.Column(db.String(80))
    title = db.Column(db.String(30))
    date_of_birth = db.Column(db.String(30))
    spouse_title = db.Column(db.String(30))
    spouse_first_names = db.Column(db.String(150))
    spouse_surname = db.Column(db.String(150))
    spouse_id_number = db.Column(db.String(30))
    spouse_date_of_birth = db.Column(db.String(30))
    residential_address = db.Column(db.Text)
    residential_postal_code = db.Column(db.String(20))
    postal_address = db.Column(db.Text)
    postal_code = db.Column(db.String(20))
    home_tel = db.Column(db.String(50))
    work_tel = db.Column(db.String(50))
    plan_choice = db.Column(db.String(50))
    extended_premium = db.Column(db.Numeric(12,2), default=0)
    total_payment = db.Column(db.Numeric(12,2), default=0)

    dependents_json = db.Column(db.Text)       # children/family dependents as JSON
    extended_family_json = db.Column(db.Text)  # extended family rows as JSON
    product_dependents_json = db.Column(db.Text)

    beneficiary_full_names = db.Column(db.String(200))
    beneficiary_title = db.Column(db.String(30))
    beneficiary_id_number = db.Column(db.String(30))
    beneficiary_date_of_birth = db.Column(db.String(30))
    beneficiary_relationship = db.Column(db.String(80))

    payment_method = db.Column(db.String(80))
    first_deduction_date = db.Column(db.String(30))
    debit_day = db.Column(db.String(20))
    bank_name = db.Column(db.String(120))
    branch_name = db.Column(db.String(120))
    branch_code = db.Column(db.String(50))
    bank_town = db.Column(db.String(120))
    account_number = db.Column(db.String(80))
    account_type = db.Column(db.String(80))
    account_holder = db.Column(db.String(180))

    employer = db.Column(db.String(180))
    salary = db.Column(db.Numeric(12,2), default=0)
    persal_no = db.Column(db.String(80))
    paypoint = db.Column(db.String(120))
    payroll_premium = db.Column(db.Numeric(12,2), default=0)
    personal_holder = db.Column(db.String(180))
    department_code = db.Column(db.String(80))

    form_template = db.Column(db.String(80))   # single_family or member_product

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    signed_at = db.Column(db.DateTime)
    signed_pdf_path = db.Column(db.String(500))
    welcome_pack_path = db.Column(db.String(500))

    # Address/geolocation and one-time signing link fields
    address_place_id = db.Column(db.String(255))
    address_lat = db.Column(db.Numeric(12,8))
    address_lng = db.Column(db.Numeric(12,8))
    sign_token = db.Column(db.String(255), unique=True)
    sign_token_created_at = db.Column(db.DateTime)
    sign_token_used_at = db.Column(db.DateTime)
    sign_token_revoked = db.Column(db.Boolean, default=False)
    popia_pdf_path = db.Column(db.String(500))
    disclosure_pdf_path = db.Column(db.String(500))

    product = db.relationship("PolicyProduct")
    agent = db.relationship("User")
    lapsed_policy = db.relationship("LapsedPolicy")
    signatures = db.relationship("ApplicationSignature", backref="application", lazy=True)

class ApplicationSignature(db.Model):
    __tablename__ = "application_signatures"
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("client_applications.id"), nullable=False)
    typed_name = db.Column(db.String(180))
    otp_code = db.Column(db.String(12))
    otp_verified = db.Column(db.Boolean, default=False)
    signature_image_path = db.Column(db.String(500))
    ip_address = db.Column(db.String(80))
    user_agent = db.Column(db.String(500))
    consent_popia = db.Column(db.Boolean, default=False)
    consent_marketing = db.Column(db.Boolean, default=False)
    consent_disclosure = db.Column(db.Boolean, default=False)
    signed_at = db.Column(db.DateTime, default=datetime.utcnow)



class ClientFicaDocument(db.Model):
    __tablename__ = "client_fica_documents"
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("client_applications.id"), nullable=False, index=True)
    document_type = db.Column(db.String(80), nullable=False)  # id_copy, proof_of_address, bank_statement, passport, permit_visa
    original_filename = db.Column(db.String(255))
    file_path = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(40), default="Needs Review")  # Needs Review, Reviewed, Rejected
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_ip = db.Column(db.String(80))
    user_agent = db.Column(db.String(500))
    application = db.relationship("ClientApplication", backref=db.backref("fica_documents", lazy=True))

class DocumentSignature(db.Model):
    __tablename__ = "document_signatures"
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("client_applications.id"), nullable=False, index=True)
    document_type = db.Column(db.String(80), nullable=False)  # application, popia, disclosure, welcome, fica
    typed_name = db.Column(db.String(180))
    signature_image_path = db.Column(db.String(500), nullable=False)
    ip_address = db.Column(db.String(80))
    user_agent = db.Column(db.String(500))
    signed_at = db.Column(db.DateTime, default=datetime.utcnow)
    application = db.relationship("ClientApplication", backref=db.backref("document_signatures", lazy=True))

class LapsedPolicy(db.Model):
    __tablename__ = "lapsed_policies"
    id = db.Column(db.Integer, primary_key=True)
    franchise = db.Column(db.String(160))
    member_id = db.Column(db.String(50))
    policy_number = db.Column(db.String(80), index=True)
    surname = db.Column(db.String(120))
    initials = db.Column(db.String(50))
    cell_number = db.Column(db.String(50))
    home_tel = db.Column(db.String(50))
    address = db.Column(db.Text)
    last_date_paid = db.Column(db.Date)
    premium_due = db.Column(db.Numeric(12,2), default=0)
    total = db.Column(db.Numeric(12,2), default=0)
    payment_method = db.Column(db.String(50))
    branch = db.Column(db.String(120))
    company_name = db.Column(db.String(160))
    id_number = db.Column(db.String(30))
    email_address = db.Column(db.String(255))
    suspense_reason = db.Column(db.Text)
    comments = db.Column(db.Text)
    assigned_agent_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    recovery_status = db.Column(db.String(80), default="Imported")
    next_action_date = db.Column(db.Date, default=date.today)
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)

class RecoveryCallLog(db.Model):
    __tablename__ = "recovery_call_logs"
    id = db.Column(db.Integer, primary_key=True)
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"), nullable=False)
    agent_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    outcome = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.Text)
    promise_to_pay_date = db.Column(db.Date)
    promise_to_pay_amount = db.Column(db.Numeric(12,2))
    follow_up_date = db.Column(db.Date)
    next_action_date = db.Column(db.Date)

    # Full application form fields
    agent_name = db.Column(db.String(150))
    agent_code = db.Column(db.String(80))
    title = db.Column(db.String(30))
    date_of_birth = db.Column(db.String(30))
    spouse_title = db.Column(db.String(30))
    spouse_first_names = db.Column(db.String(150))
    spouse_surname = db.Column(db.String(150))
    spouse_id_number = db.Column(db.String(30))
    spouse_date_of_birth = db.Column(db.String(30))
    residential_address = db.Column(db.Text)
    residential_postal_code = db.Column(db.String(20))
    postal_address = db.Column(db.Text)
    postal_code = db.Column(db.String(20))
    home_tel = db.Column(db.String(50))
    work_tel = db.Column(db.String(50))
    plan_choice = db.Column(db.String(50))
    extended_premium = db.Column(db.Numeric(12,2), default=0)
    total_payment = db.Column(db.Numeric(12,2), default=0)

    dependents_json = db.Column(db.Text)       # children/family dependents as JSON
    extended_family_json = db.Column(db.Text)  # extended family rows as JSON
    product_dependents_json = db.Column(db.Text)

    beneficiary_full_names = db.Column(db.String(200))
    beneficiary_title = db.Column(db.String(30))
    beneficiary_id_number = db.Column(db.String(30))
    beneficiary_date_of_birth = db.Column(db.String(30))
    beneficiary_relationship = db.Column(db.String(80))

    payment_method = db.Column(db.String(80))
    first_deduction_date = db.Column(db.String(30))
    debit_day = db.Column(db.String(20))
    bank_name = db.Column(db.String(120))
    branch_name = db.Column(db.String(120))
    branch_code = db.Column(db.String(50))
    bank_town = db.Column(db.String(120))
    account_number = db.Column(db.String(80))
    account_type = db.Column(db.String(80))
    account_holder = db.Column(db.String(180))

    employer = db.Column(db.String(180))
    salary = db.Column(db.Numeric(12,2), default=0)
    persal_no = db.Column(db.String(80))
    paypoint = db.Column(db.String(120))
    payroll_premium = db.Column(db.Numeric(12,2), default=0)
    personal_holder = db.Column(db.String(180))
    department_code = db.Column(db.String(80))

    form_template = db.Column(db.String(80))   # single_family or member_product

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    policy = db.relationship("LapsedPolicy")
    agent = db.relationship("User")

class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(150))
    entity_type = db.Column(db.String(100))
    entity_id = db.Column(db.String(100))
    details = db.Column(db.Text)

    # Full application form fields
    agent_name = db.Column(db.String(150))
    agent_code = db.Column(db.String(80))
    title = db.Column(db.String(30))
    date_of_birth = db.Column(db.String(30))
    spouse_title = db.Column(db.String(30))
    spouse_first_names = db.Column(db.String(150))
    spouse_surname = db.Column(db.String(150))
    spouse_id_number = db.Column(db.String(30))
    spouse_date_of_birth = db.Column(db.String(30))
    residential_address = db.Column(db.Text)
    residential_postal_code = db.Column(db.String(20))
    postal_address = db.Column(db.Text)
    postal_code = db.Column(db.String(20))
    home_tel = db.Column(db.String(50))
    work_tel = db.Column(db.String(50))
    plan_choice = db.Column(db.String(50))
    extended_premium = db.Column(db.Numeric(12,2), default=0)
    total_payment = db.Column(db.Numeric(12,2), default=0)

    dependents_json = db.Column(db.Text)       # children/family dependents as JSON
    extended_family_json = db.Column(db.Text)  # extended family rows as JSON
    product_dependents_json = db.Column(db.Text)

    beneficiary_full_names = db.Column(db.String(200))
    beneficiary_title = db.Column(db.String(30))
    beneficiary_id_number = db.Column(db.String(30))
    beneficiary_date_of_birth = db.Column(db.String(30))
    beneficiary_relationship = db.Column(db.String(80))

    payment_method = db.Column(db.String(80))
    first_deduction_date = db.Column(db.String(30))
    debit_day = db.Column(db.String(20))
    bank_name = db.Column(db.String(120))
    branch_name = db.Column(db.String(120))
    branch_code = db.Column(db.String(50))
    bank_town = db.Column(db.String(120))
    account_number = db.Column(db.String(80))
    account_type = db.Column(db.String(80))
    account_holder = db.Column(db.String(180))

    employer = db.Column(db.String(180))
    salary = db.Column(db.Numeric(12,2), default=0)
    persal_no = db.Column(db.String(80))
    paypoint = db.Column(db.String(120))
    payroll_premium = db.Column(db.Numeric(12,2), default=0)
    personal_holder = db.Column(db.String(180))
    department_code = db.Column(db.String(80))

    form_template = db.Column(db.String(80))   # single_family or member_product

    created_at = db.Column(db.DateTime, default=datetime.utcnow)



class ComplianceReview(db.Model):
    __tablename__ = "compliance_reviews"
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("client_applications.id"), nullable=False, index=True)
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"), index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    decision = db.Column(db.String(40), nullable=False)
    checklist_json = db.Column(db.Text, default="{}")
    score = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    application = db.relationship("ClientApplication", backref=db.backref("compliance_reviews", lazy=True, order_by="ComplianceReview.created_at.desc()"))
    lapsed_policy = db.relationship("LapsedPolicy", backref=db.backref("compliance_reviews", lazy=True, order_by="ComplianceReview.created_at.desc()"))
    reviewer = db.relationship("User")

class QRTrustedDevice(db.Model):
    __tablename__ = "qr_trusted_devices"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    device_token_hash = db.Column(db.String(255), unique=True, nullable=False, index=True)
    device_name = db.Column(db.String(180))
    first_ip = db.Column(db.String(80))
    last_ip = db.Column(db.String(80))
    user_agent = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime, nullable=False)
    active = db.Column(db.Boolean, default=True, index=True)
    user = db.relationship("User")

    @property
    def is_expired(self):
        return datetime.utcnow() > self.expires_at

class QRLoginToken(db.Model):
    __tablename__ = "qr_login_tokens"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(128), unique=True, nullable=False, index=True)
    status = db.Column(db.String(30), default="pending", index=True)  # pending, approved, rejected, expired, used
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    approved_at = db.Column(db.DateTime)
    used_at = db.Column(db.DateTime)
    approved_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    desktop_ip = db.Column(db.String(80))
    desktop_user_agent = db.Column(db.String(500))
    approval_ip = db.Column(db.String(80))
    approval_user_agent = db.Column(db.String(500))
    user = db.relationship("User")

    @property
    def is_expired(self):
        return datetime.utcnow() > self.expires_at


class TelesalesScriptSession(db.Model):
    __tablename__ = "telesales_script_sessions"
    id = db.Column(db.Integer, primary_key=True)
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"), index=True)
    application_id = db.Column(db.Integer, db.ForeignKey("client_applications.id"), index=True)
    agent_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    branch = db.Column(db.String(120))
    client_name = db.Column(db.String(220))
    client_cell = db.Column(db.String(50))
    client_email = db.Column(db.String(255))
    policy_number = db.Column(db.String(80))
    script_type = db.Column(db.String(50), default="new")  # new or reinstatement
    current_step = db.Column(db.Integer, default=1)
    status = db.Column(db.String(50), default="In Progress")  # In Progress, Completed, Blocked
    blocked_reason = db.Column(db.Text)
    answers_json = db.Column(db.Text, default="{}")
    qa_score = db.Column(db.Integer, default=0)
    qa_result = db.Column(db.String(20))
    pdf_path = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    lapsed_policy = db.relationship("LapsedPolicy")
    application = db.relationship("ClientApplication")
    agent = db.relationship("User")

class SystemSetting(db.Model):
    __tablename__ = "system_settings"
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(80), nullable=False, index=True)
    key = db.Column(db.String(120), nullable=False, index=True)
    value = db.Column(db.Text, default="")
    description = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.relationship("User")
    __table_args__ = (db.UniqueConstraint('category', 'key', name='uq_system_setting_category_key'),)

class LoginAttempt(db.Model):
    __tablename__ = "login_attempts"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), index=True)
    ip_address = db.Column(db.String(80), index=True)
    success = db.Column(db.Boolean, default=False)
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

class CallSummary(db.Model):
    __tablename__ = "call_summaries"
    id = db.Column(db.Integer, primary_key=True)
    call_log_id = db.Column(db.Integer, db.ForeignKey("recovery_call_logs.id"), nullable=False, index=True)
    summary = db.Column(db.Text)
    next_best_action = db.Column(db.String(255))
    risk_flags = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    call_log = db.relationship("RecoveryCallLog")

class LeadDistributionRule(db.Model):
    __tablename__ = "lead_distribution_rules"
    id = db.Column(db.Integer, primary_key=True)
    branch = db.Column(db.String(120), index=True)
    method = db.Column(db.String(40), default="round_robin")
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AgentTarget(db.Model):
    __tablename__ = "agent_targets"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    target_date = db.Column(db.Date, nullable=False, default=date.today)
    calls_target = db.Column(db.Integer, default=0)
    sales_target = db.Column(db.Integer, default=0)
    premium_target = db.Column(db.Numeric(12,2), default=0)
    user = db.relationship("User")

class CommissionRule(db.Model):
    __tablename__ = "commission_rules"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    role_name = db.Column(db.String(80), default="agent")
    flat_amount_per_sale = db.Column(db.Numeric(12,2), default=0)
    percentage_of_premium = db.Column(db.Numeric(6,2), default=0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class SalesTarget(db.Model):
    __tablename__ = "sales_targets"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    branch = db.Column(db.String(120), index=True)
    month = db.Column(db.String(7), index=True)  # YYYY-MM
    calls_target = db.Column(db.Integer, default=0)
    sales_target = db.Column(db.Integer, default=0)
    premium_target = db.Column(db.Numeric(12,2), default=0)
    user = db.relationship("User")

class CallRecording(db.Model):
    __tablename__ = "call_recordings"
    id = db.Column(db.Integer, primary_key=True)
    call_log_id = db.Column(db.Integer, db.ForeignKey("recovery_call_logs.id"), index=True)
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"), index=True)
    agent_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    file_path = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(255))
    duration_seconds = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    call_log = db.relationship("RecoveryCallLog")
    agent = db.relationship("User")

class ComplianceFlag(db.Model):
    __tablename__ = "compliance_flags"
    id = db.Column(db.Integer, primary_key=True)
    call_log_id = db.Column(db.Integer, db.ForeignKey("recovery_call_logs.id"), index=True)
    severity = db.Column(db.String(30), default="medium")
    flag_type = db.Column(db.String(120))
    details = db.Column(db.Text)
    resolved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    call_log = db.relationship("RecoveryCallLog")

class CommunicationCampaign(db.Model):
    __tablename__ = "communication_campaigns"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    subject = db.Column(db.String(255))
    message_body = db.Column(db.Text, nullable=False)
    whatsapp_template_name = db.Column(db.String(160))
    whatsapp_template_language = db.Column(db.String(20), default="en_US")
    template_category = db.Column(db.String(40), default="MARKETING")
    template_type = db.Column(db.String(40), default="MEDIA_INTERACTIVE")
    template_footer = db.Column(db.String(60))
    template_buttons_json = db.Column(db.Text)
    template_allow_category_change = db.Column(db.Boolean, default=True, nullable=False)
    image_filename = db.Column(db.String(255))
    image_url = db.Column(db.String(1000))
    image_data = db.Column(db.LargeBinary)
    image_mimetype = db.Column(db.String(100))
    audience_type = db.Column(db.String(20), default="group", nullable=False)
    template_status = db.Column(db.String(30), default="Pending", nullable=False)
    template_checked_at = db.Column(db.DateTime)
    template_status_error = db.Column(db.String(1000))
    template_approved_at = db.Column(db.DateTime)
    template_approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    template_provider_id = db.Column(db.String(160))
    template_submitted_at = db.Column(db.DateTime)
    template_approval_notified_at = db.Column(db.DateTime)
    send_whatsapp = db.Column(db.Boolean, default=True, nullable=False)
    send_email = db.Column(db.Boolean, default=True, nullable=False)
    branch = db.Column(db.String(120))
    status = db.Column(db.String(40), default="Draft", nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    sent_at = db.Column(db.DateTime)
    scheduled_at = db.Column(db.DateTime, index=True)
    queue_status = db.Column(db.String(30), default="idle", nullable=False, index=True)
    archived_at = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime)
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    template_approved_by = db.relationship("User", foreign_keys=[template_approved_by_id])

class CampaignRecipient(db.Model):
    __tablename__ = "campaign_recipients"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("communication_campaigns.id"), nullable=False, index=True)
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"), nullable=False, index=True)
    secure_token = db.Column(db.String(128), unique=True, nullable=False, index=True)
    whatsapp_status = db.Column(db.String(40), default="Not Sent")
    email_status = db.Column(db.String(40), default="Not Sent")
    response_type = db.Column(db.String(40))
    response_channel = db.Column(db.String(30))
    responded_at = db.Column(db.DateTime)
    callback_created = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    campaign = db.relationship("CommunicationCampaign", backref=db.backref("recipients", lazy=True))
    policy = db.relationship("LapsedPolicy")
    __table_args__ = (db.UniqueConstraint("campaign_id", "lapsed_policy_id", name="uq_campaign_policy"),)

class ContactCommunicationPreference(db.Model):
    __tablename__ = "contact_communication_preferences"
    id = db.Column(db.Integer, primary_key=True)
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"), unique=True, nullable=False, index=True)
    telephone_allowed = db.Column(db.Boolean, default=True, nullable=False)
    whatsapp_allowed = db.Column(db.Boolean, default=True, nullable=False)
    email_allowed = db.Column(db.Boolean, default=True, nullable=False)
    opted_out_all = db.Column(db.Boolean, default=False, nullable=False)
    opted_out_at = db.Column(db.DateTime)
    opt_out_source = db.Column(db.String(40))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    policy = db.relationship("LapsedPolicy", backref=db.backref("communication_preference", uselist=False))

class ContactSuppression(db.Model):
    __tablename__ = "contact_suppressions"
    id = db.Column(db.Integer, primary_key=True)
    phone_hash = db.Column(db.String(128), index=True)
    email_hash = db.Column(db.String(128), index=True)
    reason = db.Column(db.String(160), default="Client opted out")
    source = db.Column(db.String(40))
    campaign_id = db.Column(db.Integer, db.ForeignKey("communication_campaigns.id"))
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"))
    suppressed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class AgentNotification(db.Model):
    __tablename__ = "agent_notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    message = db.Column(db.Text)
    notification_type = db.Column(db.String(50), default="info")
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.Integer)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    user = db.relationship("User")

class CommunicationFollowUp(db.Model):
    __tablename__ = "communication_follow_ups"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("communication_campaigns.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("campaign_recipients.id"), nullable=False, index=True)
    due_at = db.Column(db.DateTime, nullable=False, index=True)
    channel = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(30), default="Pending", nullable=False, index=True)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text)
    processed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    campaign = db.relationship("CommunicationCampaign")
    recipient = db.relationship("CampaignRecipient")

class CommunicationEvent(db.Model):
    __tablename__ = "communication_events"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("communication_campaigns.id"), index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("campaign_recipients.id"), index=True)
    lapsed_policy_id = db.Column(db.Integer, db.ForeignKey("lapsed_policies.id"), index=True)
    event_type = db.Column(db.String(60), nullable=False, index=True)
    channel = db.Column(db.String(30))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    campaign = db.relationship("CommunicationCampaign")
    recipient = db.relationship("CampaignRecipient")
    policy = db.relationship("LapsedPolicy")

class WhatsAppContact(db.Model):
    __tablename__ = "whatsapp_contacts"
    id = db.Column(db.Integer, primary_key=True)
    wa_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    phone_number = db.Column(db.String(40), nullable=False, index=True)
    display_name = db.Column(db.String(180))
    email = db.Column(db.String(255))
    branch = db.Column(db.String(120), index=True)
    assigned_agent_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    status = db.Column(db.String(40), default="New", nullable=False, index=True)
    tags = db.Column(db.String(500), default="")
    notes = db.Column(db.Text)
    opted_out = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    assigned_agent = db.relationship("User")

class WhatsAppConversation(db.Model):
    __tablename__ = "whatsapp_conversations"
    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("whatsapp_contacts.id"), nullable=False, index=True)
    assigned_agent_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    status = db.Column(db.String(30), default="Open", nullable=False, index=True)
    priority = db.Column(db.String(20), default="Normal", nullable=False)
    unread_count = db.Column(db.Integer, default=0, nullable=False)
    last_message_preview = db.Column(db.String(500))
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime)
    contact = db.relationship("WhatsAppContact", backref=db.backref("conversations", lazy=True))
    assigned_agent = db.relationship("User")

class WhatsAppMessage(db.Model):
    __tablename__ = "whatsapp_messages"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("whatsapp_conversations.id"), nullable=False, index=True)
    provider_message_id = db.Column(db.String(255), unique=True, index=True)
    direction = db.Column(db.String(12), nullable=False, index=True)  # inbound/outbound
    message_type = db.Column(db.String(30), default="text", nullable=False)
    body = db.Column(db.Text)
    media_id = db.Column(db.String(255))
    media_url = db.Column(db.String(1000))
    media_mime_type = db.Column(db.String(120))
    status = db.Column(db.String(30), default="received", nullable=False, index=True)
    error_message = db.Column(db.Text)
    sender_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    raw_payload = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    delivered_at = db.Column(db.DateTime)
    read_at = db.Column(db.DateTime)
    conversation = db.relationship("WhatsAppConversation", backref=db.backref("messages", lazy=True, order_by="WhatsAppMessage.created_at"))
    sender_user = db.relationship("User")

class WhatsAppWebhookEvent(db.Model):
    __tablename__ = "whatsapp_webhook_events"
    id = db.Column(db.Integer, primary_key=True)
    event_key = db.Column(db.String(255), unique=True, index=True)
    payload = db.Column(db.Text, nullable=False)
    processed = db.Column(db.Boolean, default=False, nullable=False, index=True)
    error = db.Column(db.Text)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    processed_at = db.Column(db.DateTime)

class WhatsAppMediaAsset(db.Model):
    __tablename__ = "whatsapp_media_assets"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("communication_campaigns.id"), index=True)
    filename = db.Column(db.String(255))
    mime_type = db.Column(db.String(120))
    purpose = db.Column(db.String(60), default="template_header", nullable=False, index=True)
    storage_provider = db.Column(db.String(40), default="database", nullable=False)
    provider_asset_id = db.Column(db.String(255))
    public_url = db.Column(db.String(1200))
    status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    last_error = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    campaign = db.relationship("CommunicationCampaign")
    created_by = db.relationship("User")


class WhatsAppTemplate(db.Model):
    __tablename__ = "whatsapp_templates"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("communication_campaigns.id"), unique=True, nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    language = db.Column(db.String(20), default="en", nullable=False, index=True)
    category = db.Column(db.String(40), default="MARKETING", nullable=False)
    body_text = db.Column(db.Text, nullable=False)
    footer_text = db.Column(db.String(60))
    buttons_json = db.Column(db.Text)
    components_json = db.Column(db.Text)
    allow_category_change = db.Column(db.Boolean, default=True, nullable=False)
    header_image_url = db.Column(db.String(1200))
    button_one_text = db.Column(db.String(80))
    button_two_text = db.Column(db.String(80))
    status = db.Column(db.String(40), default="Draft", nullable=False, index=True)
    provider_template_id = db.Column(db.String(255), index=True)
    provider_request_id = db.Column(db.String(255))
    quality_rating = db.Column(db.String(80))
    rejection_reason = db.Column(db.Text)
    last_error = db.Column(db.Text)
    last_provider_response = db.Column(db.Text)
    retry_count = db.Column(db.Integer, default=0, nullable=False)
    submitted_at = db.Column(db.DateTime)
    approved_at = db.Column(db.DateTime)
    approval_notified_at = db.Column(db.DateTime)
    last_checked_at = db.Column(db.DateTime)
    next_check_at = db.Column(db.DateTime, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    campaign = db.relationship("CommunicationCampaign", backref=db.backref("enterprise_template", uselist=False))
    created_by = db.relationship("User")


class WhatsAppProviderJob(db.Model):
    __tablename__ = "whatsapp_provider_jobs"
    id = db.Column(db.Integer, primary_key=True)
    job_type = db.Column(db.String(60), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("communication_campaigns.id"), nullable=False, index=True)
    status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    run_after = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=5, nullable=False)
    last_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    campaign = db.relationship("CommunicationCampaign")

class WhatsAppMediaVersion(db.Model):
    __tablename__ = "whatsapp_media_versions"
    id = db.Column(db.Integer, primary_key=True)
    media_asset_id = db.Column(db.Integer, db.ForeignKey("whatsapp_media_assets.id"), nullable=False, index=True)
    version_number = db.Column(db.Integer, nullable=False)
    filename = db.Column(db.String(255))
    mime_type = db.Column(db.String(120))
    file_data = db.Column(db.LargeBinary)
    public_url = db.Column(db.String(1200))
    checksum = db.Column(db.String(64), index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    media_asset = db.relationship("WhatsAppMediaAsset", backref=db.backref("versions", lazy=True, order_by="WhatsAppMediaVersion.version_number.desc()"))
    created_by = db.relationship("User")
    __table_args__ = (db.UniqueConstraint("media_asset_id", "version_number", name="uq_whatsapp_media_version"),)


class WhatsAppProviderLog(db.Model):
    __tablename__ = "whatsapp_provider_logs"
    id = db.Column(db.Integer, primary_key=True)
    operation = db.Column(db.String(80), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("communication_campaigns.id"), index=True)
    provider = db.Column(db.String(80), default="meta", nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, index=True)
    request_summary = db.Column(db.Text)
    response_summary = db.Column(db.Text)
    error = db.Column(db.Text)
    duration_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    campaign = db.relationship("CommunicationCampaign")


class WhatsAppAuditEvent(db.Model):
    __tablename__ = "whatsapp_audit_events"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(100), nullable=False, index=True)
    entity_type = db.Column(db.String(60), nullable=False, index=True)
    entity_id = db.Column(db.Integer, index=True)
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    user = db.relationship("User")
