from datetime import datetime, timedelta
from io import BytesIO
import base64
import hashlib
import secrets

import qrcode
from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash, make_response
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import AuditLog, QRLoginToken, QRTrustedDevice, User


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

QR_EXPIRY_SECONDS = 90
TRUSTED_DEVICE_DAYS = 180
TRUSTED_DEVICE_COOKIE = "mf_qr_trusted_device"
ALLOWED_QR_ROLES = {"admin", "manager", "supervisor", "agent", "user", "staff"}


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _user_agent():
    return (request.headers.get("User-Agent") or "")[:500]


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _audit(user_id, action, details):
    try:
        db.session.add(AuditLog(user_id=user_id, action=action, entity_type="auth", entity_id="qr_login", details=details))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _role_allowed(user):
    role_name = (user.role.name if getattr(user, "role", None) else "").lower().strip()
    return not role_name or role_name in ALLOWED_QR_ROLES




def _is_admin_user(user):
    role_name = (user.role.name if getattr(user, "role", None) else "").lower().strip()
    return role_name == "admin"

def _admin_required():
    if not current_user.is_authenticated or not _is_admin_user(current_user):
        flash("Admin access required.", "danger")
        return redirect(url_for("main.dashboard"))
    return None

def _device_type(user_agent):
    ua = (user_agent or "").lower()
    if "iphone" in ua or "ipad" in ua:
        return "iPhone / iPad"
    if "android" in ua:
        return "Android phone"
    if "windows" in ua:
        return "Windows device"
    if "macintosh" in ua or "mac os" in ua:
        return "Apple Mac device"
    if "linux" in ua:
        return "Linux device"
    return "Unknown device"

def _trusted_device_from_cookie():
    raw_token = request.cookies.get(TRUSTED_DEVICE_COOKIE)
    if not raw_token:
        return None
    device = QRTrustedDevice.query.filter_by(device_token_hash=_hash_token(raw_token), active=True).first()
    if not device or device.is_expired:
        return None
    user = db.session.get(User, device.user_id)
    if not user or not user.active or not _role_allowed(user):
        return None
    return device


def _set_trusted_device_cookie(response, raw_token):
    response.set_cookie(
        TRUSTED_DEVICE_COOKIE,
        raw_token,
        max_age=TRUSTED_DEVICE_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=request.is_secure,
        samesite="Lax",
    )
    return response


def _approve_qr_with_user(qr_token, user, via):
    qr_token.status = "approved"
    qr_token.approved_user_id = user.id
    qr_token.approved_at = datetime.utcnow()
    qr_token.approval_ip = _client_ip()
    qr_token.approval_user_agent = _user_agent()
    db.session.commit()
    _audit(user.id, "QR_LOGIN_APPROVED", f"QR login approved via {via} from phone IP {_client_ip()}")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(email=request.form.get("email", "").lower().strip()).first()
        if user and user.check_password(request.form.get("password", "")) and user.active:
            login_user(user)
            return redirect(url_for("main.dashboard"))
        flash("Invalid login details", "danger")
    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/qr/start", methods=["POST"])
def qr_start():
    token = secrets.token_urlsafe(48)
    qr_token = QRLoginToken(
        token=token,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(seconds=QR_EXPIRY_SECONDS),
        desktop_ip=_client_ip(),
        desktop_user_agent=_user_agent(),
    )
    db.session.add(qr_token)
    db.session.commit()

    approve_url = url_for("auth.qr_approve", token=token, _external=True)
    img = qrcode.make(approve_url)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    image_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")

    return jsonify({
        "token": token,
        "qr_image": f"data:image/png;base64,{image_b64}",
        "expires_in": QR_EXPIRY_SECONDS,
    })


@auth_bp.route("/qr/status/<token>")
def qr_status(token):
    qr_token = QRLoginToken.query.filter_by(token=token).first()
    if not qr_token:
        return jsonify({"status": "invalid"}), 404

    if qr_token.desktop_ip != _client_ip() or qr_token.desktop_user_agent != _user_agent():
        return jsonify({"status": "blocked"}), 403

    if qr_token.status == "pending" and qr_token.is_expired:
        qr_token.status = "expired"
        db.session.commit()

    if qr_token.status == "approved":
        user = db.session.get(User, qr_token.approved_user_id)
        if not user or not user.active or not _role_allowed(user):
            qr_token.status = "rejected"
            db.session.commit()
            return jsonify({"status": "rejected"})
        login_user(user)
        qr_token.status = "used"
        qr_token.used_at = datetime.utcnow()
        db.session.commit()
        _audit(user.id, "QR_LOGIN_USED", f"Desktop QR login completed from IP {_client_ip()}")
        return jsonify({"status": "approved", "redirect": url_for("main.dashboard")})

    return jsonify({"status": qr_token.status})


@auth_bp.route("/qr/approve/<token>", methods=["GET", "POST"])
def qr_approve(token):
    qr_token = QRLoginToken.query.filter_by(token=token).first()
    if not qr_token:
        return render_template("auth/qr_approve.html", state="invalid")

    if qr_token.status != "pending" or qr_token.is_expired:
        if qr_token.status == "pending":
            qr_token.status = "expired"
            db.session.commit()
        return render_template("auth/qr_approve.html", state=qr_token.status)

    trusted_device = _trusted_device_from_cookie()
    if trusted_device:
        user = db.session.get(User, trusted_device.user_id)
        trusted_device.last_ip = _client_ip()
        trusted_device.last_used_at = datetime.utcnow()
        _approve_qr_with_user(qr_token, user, "trusted device")
        return render_template("auth/qr_approve.html", state="trusted_approved", user=user)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "reject":
            qr_token.status = "rejected"
            db.session.commit()
            return render_template("auth/qr_approve.html", state="rejected")

        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password) or not user.active or not _role_allowed(user):
            flash("Access denied. Use an active TeleSales user account.", "danger")
            return render_template("auth/qr_approve.html", state="pair_required", token=qr_token)

        raw_device_token = secrets.token_urlsafe(48)
        device = QRTrustedDevice(
            user_id=user.id,
            device_token_hash=_hash_token(raw_device_token),
            device_name=request.form.get("device_name", "Mobile device")[:180] or "Mobile device",
            first_ip=_client_ip(),
            last_ip=_client_ip(),
            user_agent=_user_agent(),
            last_used_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=TRUSTED_DEVICE_DAYS),
            active=True,
        )
        db.session.add(device)
        _approve_qr_with_user(qr_token, user, "first-time device pairing")
        response = make_response(render_template("auth/qr_approve.html", state="paired_approved", user=user))
        _set_trusted_device_cookie(response, raw_device_token)
        _audit(user.id, "QR_TRUSTED_DEVICE_CREATED", f"Trusted QR device created from IP {_client_ip()}")
        return response

    return render_template("auth/qr_approve.html", state="pair_required", token=qr_token)


@auth_bp.route("/qr/remove-trusted-device", methods=["POST"])
def qr_remove_trusted_device():
    raw_token = request.cookies.get(TRUSTED_DEVICE_COOKIE)
    response = make_response(redirect(url_for("auth.login")))
    if raw_token:
        device = QRTrustedDevice.query.filter_by(device_token_hash=_hash_token(raw_token), active=True).first()
        if device:
            device.active = False
            db.session.commit()
            _audit(device.user_id, "QR_TRUSTED_DEVICE_REMOVED", f"Trusted QR device removed from IP {_client_ip()}")
        response.delete_cookie(TRUSTED_DEVICE_COOKIE)
    flash("This phone is no longer trusted for QR login.", "info")
    return response


@auth_bp.route("/admin/trusted-devices")
@login_required
def admin_trusted_devices():
    blocked = _admin_required()
    if blocked:
        return blocked

    devices = (QRTrustedDevice.query
        .order_by(QRTrustedDevice.active.desc(), QRTrustedDevice.last_used_at.desc().nullslast(), QRTrustedDevice.created_at.desc())
        .all())
    users = {u.id: u for u in User.query.all()}
    return render_template("auth/admin_trusted_devices.html", devices=devices, users=users, device_type=_device_type, now=datetime.utcnow())


@auth_bp.route("/admin/trusted-devices/<int:device_id>/revoke", methods=["POST"])
@login_required
def admin_revoke_trusted_device(device_id):
    blocked = _admin_required()
    if blocked:
        return blocked

    device = db.session.get(QRTrustedDevice, device_id)
    if not device:
        flash("Trusted device not found.", "warning")
        return redirect(url_for("auth.admin_trusted_devices"))

    device.active = False
    device.expires_at = datetime.utcnow()
    db.session.commit()
    _audit(current_user.id, "QR_TRUSTED_DEVICE_REVOKED_BY_ADMIN", f"Admin revoked trusted device {device.id} for user {device.user_id}")
    flash("Trusted device revoked. The phone must be paired again before QR auto-login works.", "success")
    return redirect(url_for("auth.admin_trusted_devices"))


@auth_bp.route("/admin/trusted-devices/<int:device_id>/force-repair", methods=["POST"])
@login_required
def admin_force_repair_device(device_id):
    blocked = _admin_required()
    if blocked:
        return blocked

    device = db.session.get(QRTrustedDevice, device_id)
    if not device:
        flash("Trusted device not found.", "warning")
        return redirect(url_for("auth.admin_trusted_devices"))

    device.active = False
    device.expires_at = datetime.utcnow()
    db.session.commit()
    _audit(current_user.id, "QR_TRUSTED_DEVICE_FORCE_REPAIR", f"Admin forced re-pairing for device {device.id} user {device.user_id}")
    flash("Force re-pairing applied for this device.", "success")
    return redirect(url_for("auth.admin_trusted_devices"))


@auth_bp.route("/admin/trusted-devices/user/<int:user_id>/force-repair", methods=["POST"])
@login_required
def admin_force_repair_user_devices(user_id):
    blocked = _admin_required()
    if blocked:
        return blocked

    devices = QRTrustedDevice.query.filter_by(user_id=user_id, active=True).all()
    for device in devices:
        device.active = False
        device.expires_at = datetime.utcnow()
    db.session.commit()
    _audit(current_user.id, "QR_TRUSTED_USER_FORCE_REPAIR", f"Admin forced re-pairing for all trusted devices for user {user_id}; count={len(devices)}")
    flash(f"Force re-pairing applied to {len(devices)} active device(s) for this user.", "success")
    return redirect(url_for("auth.admin_trusted_devices"))
