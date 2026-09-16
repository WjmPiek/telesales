"""Capture the official FIC search before releasing a new signing link."""
import hashlib
import json
import os
from threading import Lock
from datetime import datetime, timedelta
from pathlib import Path
from flask import current_app
from app import db
from app.models import ApplicationScreening
from app.services.client_storage import application_folder, store_document

_capture_lock=Lock()

FIC_URL='https://tfs.fic.gov.za/Pages/Search'


def fingerprint(a):
    return hashlib.sha256((' '.join((a.first_names or '',a.surname or '',a.id_number or '')).strip().upper()).encode()).hexdigest()


def latest(a):
    return ApplicationScreening.query.filter_by(application_id=a.id,identity_hash=fingerprint(a)).order_by(ApplicationScreening.id.desc()).first()


def capture_person_search(identity, name, folder, prefix):
    if not _capture_lock.acquire(blocking=False):
        raise RuntimeError('Another screening is running; retry shortly')
    try:
        return _capture_person_search(identity,name,folder,prefix)
    finally:
        _capture_lock.release()


def _capture_person_search(identity, name, folder, prefix):
    # The website itself displays "No results" even on HTTP errors. Inspect the
    # actual successful JSON response so an outage can never be counted as clear.
    from playwright.sync_api import sync_playwright
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(Path(current_app.root_path).parent/'.browsers'))
    result=[];evidence=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':1280,'height':1000},locale='en-ZA',timezone_id='Africa/Johannesburg')
            page.set_default_timeout(25000)
            page.goto(FIC_URL,wait_until='domcontentloaded',timeout=45000)
            page.locator('#SearchPersonButton').wait_for(state='visible')
            # Separate searches avoid a restrictive combined query concealing a name match.
            for kind,value in [('id',identity),('name',name)]:
                page.locator('input[id$="PersonNameTextBox"]').fill(value if kind=='name' else '')
                page.locator('input[id$="IdentificationNumberTextBox"]').fill(value if kind=='id' else '')
                with page.expect_response(lambda r:'/web/api/search/searchperson?' in r.url,timeout=30000) as response:
                    page.locator('#SearchPersonButton').click()
                response=response.value
                if response.status!=200:raise RuntimeError('FIC search request failed')
                rows=response.json()
                if not isinstance(rows,list):raise RuntimeError('Unexpected FIC response')
                if rows:
                    page.locator('#PersonDataTable_wrapper').wait_for(state='visible')
                    page.wait_for_function("expected => window.jQuery && JSON.stringify(jQuery('#PersonDataTable').DataTable().rows({order:'index'}).data().toArray()) === JSON.stringify(expected)",arg=rows)
                else:
                    page.locator('#PersonPlaceHolderSearch').filter(has_text='No results').wait_for(state='visible')
                path=Path(folder)/f'{prefix}-{kind}.png'
                page.screenshot(path=str(path),full_page=True)
                evidence.append(str(path))
                result.append({'search':kind,'query':value,'results':rows})
        finally:browser.close()
    return result,evidence


def ensure_screened(a, force=False):
    record=latest(a)
    fresh=record and record.checked_at>datetime.utcnow()-timedelta(hours=24)
    if fresh and not force:
        if record.status in {'No results','Reviewed'}:return True,[]
        if record.status=='Needs review':return False,['FIC returned possible matches. Staff must review the saved screening evidence before sending.']
    if not (a.id_number or '').strip() or not (a.first_names and a.surname):
        return False,['Client ID/passport, first names and surname are required for FIC screening.']
    record=ApplicationScreening(application_id=a.id,identity_hash=fingerprint(a),status='Checking')
    db.session.add(record);db.session.flush()
    folder=application_folder(a);prefix=f'fic-screening-{record.id}'
    try:
        results,paths=capture_person_search(a.id_number.strip(),f'{a.first_names} {a.surname}',folder,prefix)
        record.status='Needs review' if any(item['results'] for item in results) else 'No results'
        record.results_json=json.dumps(results)
        result_path=Path(folder)/(prefix+'.json')
        result_path.write_text(json.dumps({'source':FIC_URL,'searched_at_utc':record.checked_at.isoformat(),'searches':results},indent=2),encoding='utf-8')
        paths.append(str(result_path))
        for path in paths:store_document(a,path)
        record.evidence_json=json.dumps([Path(path).name for path in paths])
    except Exception as exc:
        record.status='Error'
        current_app.logger.warning('FIC screening unavailable for application %s (%s)',a.id,type(exc).__name__)
    db.session.commit()
    if record.status=='No results':return True,[]
    if record.status=='Needs review':return False,['FIC returned possible matches. Open the client file screening evidence for staff review.']
    return False,['FIC screening could not be completed. No signing link was sent. Retry from the application screening panel.']
