import importlib.util
from pathlib import Path
from unittest.mock import patch
from flask import Flask
from flask_login import LoginManager,UserMixin
import pytest

spec=importlib.util.spec_from_file_location('insurance_module_gate',Path(__file__).resolve().parents[1]/'app/module_license.py')
gate=importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_existing_managed_sessions_blocked_after_deactivation():
    app=Flask(__name__)
    app.secret_key='test'
    manager=LoginManager(app)
    class User(UserMixin):
        id='1'
        email='holder@example.com'
    manager.user_loader(lambda _id:User())
    app.before_request(gate.check_session_license)
    app.add_url_rule('/private',view_func=lambda:'private')
    client=app.test_client()
    with client.session_transaction() as s:s['_user_id']='1'
    with patch.object(gate,'entitlement',return_value={'managed':True,'active':True}):
        assert client.get('/private').status_code==200
    with patch.object(gate,'entitlement',return_value={'managed':True,'active':False}):
        assert client.get('/private').status_code==403
    with patch.object(gate,'entitlement',return_value={'managed':False,'active':False}):
        assert client.get('/private').status_code==200
        with client.session_transaction() as s:s['martins_module_login']=True
        assert client.get('/private').status_code==403


def test_entitlement_is_signed_bounded_and_expires_with_trial(monkeypatch):
    app=Flask(__name__)
    gate._cache.clear()
    monkeypatch.setenv('INSURANCE_LAUNCH_SECRET','test-secret')
    clock=[100]
    monkeypatch.setattr(gate.time,'time',lambda:clock[0])
    calls=[]
    class Reply:
        def raise_for_status(self):pass
        def json(self):return {'active':clock[0]<105,'managed':True,'expires_at':105}
    def post(url,**kwargs):
        assert kwargs['timeout']==(2,3)
        assert gate.URLSafeTimedSerializer('test-secret',salt='martins-module-access-v1').loads(kwargs['json']['token'])['email']=='holder@example.com'
        calls.append(1)
        return Reply()
    monkeypatch.setattr(gate.requests,'post',post)
    with app.test_request_context():
        assert gate.entitlement('holder@example.com')['active']
        clock[0]=104
        gate.entitlement('holder@example.com')
        assert len(calls)==1
        clock[0]=105
        assert not gate.entitlement('holder@example.com')['active']
        assert len(calls)==2


def test_network_failure_fails_closed(monkeypatch):
    app=Flask(__name__)
    monkeypatch.setenv('INSURANCE_LAUNCH_SECRET','test-secret')
    gate._cache.clear()
    def fail(*args,**kwargs): raise gate.requests.Timeout()
    monkeypatch.setattr(gate.requests,'post',fail)
    from werkzeug.exceptions import ServiceUnavailable
    with app.test_request_context(),pytest.raises(ServiceUnavailable):
        gate.entitlement('holder@example.com')
