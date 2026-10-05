"""One approval rule shared by password, SSO, QR and existing sessions."""
OWNER_EMAIL = 'wjm@martinsdirect.com'


def is_owner(user):
    return bool(user and (user.email or '').strip().lower() == OWNER_EMAIL)


def can_login(user):
    return bool(user and user.active and (is_owner(user) or (
        user.owner_approved_at and user.role and
        user.role.name.lower().replace('_', ' ') != 'pending')))
