"""Recheck Martins entitlements for franchise holders, including existing sessions."""
import os
import time
from threading import Lock
import requests
from flask import abort, request, session
from flask_login import current_user
from itsdangerous import URLSafeTimedSerializer

_cache = {}
_lock = Lock()


def entitlement(email):
    email = email.strip().lower()
    current = time.time()
    with _lock:
        cached = _cache.get(email)
        if cached and cached[0] > current:
            return cached[1]
    secret = os.getenv('INSURANCE_LAUNCH_SECRET','').strip()
    if not secret:
        abort(503, description='Module licensing is not configured. Contact Head Office.')
    token = URLSafeTimedSerializer(secret,salt='martins-module-access-v1').dumps({'email':email,'module':'insurance_app:view'})
    try:
        response = requests.post('https://martinssystem.co.za/api/module-access',
                                 json={'module':'insurance_app:view','token':token},timeout=(2,3))
        response.raise_for_status()
        result = response.json()
        if not isinstance(result,dict) or 'active' not in result:
            raise ValueError('Invalid licensing response')
    except (requests.RequestException,ValueError):
        abort(503, description='Cannot verify module access. Please try again shortly.')
    until = current+15
    if result.get('active') and result.get('expires_at'):
        until = min(until,float(result['expires_at']))
    with _lock:
        if len(_cache)>2000:
            _cache.clear()
        _cache[email]=(until,result)
    return result


def check_session_license():
    if request.endpoint in {'static','auth.logout'} or not current_user.is_authenticated:
        return
    # Standalone public policy applicants retain their existing approval rules.
    # A holder known to Martins, or signed in via Martins, must have a licence.
    result = entitlement(current_user.email)
    if (result.get('managed') or session.get('martins_module_login')) and not result['active']:
        abort(403, description='Insurance Applications is inactive. Open My Modules in the Martins system and enter a paid activation code, or contact Head Office.')
