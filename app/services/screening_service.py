"""Employee screening evidence is required before releasing a signing link."""
import hashlib
import json
from app import db
from app.models import ApplicationScreening, ClientStoredFile

FIC_URL='https://tfs.fic.gov.za/Pages/Search'

def fingerprint(a):
    return hashlib.sha256((' '.join((a.first_names or '',a.surname or '',a.id_number or '')).strip().upper()).encode()).hexdigest()

def latest(a):
    return ApplicationScreening.query.filter_by(application_id=a.id,identity_hash=fingerprint(a)).order_by(ApplicationScreening.id.desc()).first()

def ensure_screened(a, force=False):
    record=latest(a)
    if record and record.status in {'Employee checked','Reviewed'}:
        paths=json.loads(record.evidence_json or '[]')
        if paths and ClientStoredFile.query.filter(ClientStoredFile.application_id==a.id,ClientStoredFile.relative_path.in_(paths)).count()==len(paths):
            return True,[]
    return False,['An employee must complete the FIC check and upload the result screenshot on the application FIC screening page before sending. Possible matches require administrator review.']

def save_employee_check(a, uploads, outcome, notes, checked_at, user_id):
    from datetime import datetime, timedelta
    from PIL import Image, UnidentifiedImageError
    from io import BytesIO
    from pathlib import Path
    from app.services.client_storage import application_folder, store_document
    from app.models import AuditLog
    if not a.id_number or not a.first_names or not a.surname:
        raise ValueError('Complete the client name and ID number first.')
    if outcome not in {'no_match','possible_match'} or not 10<=len(notes)<=3000:
        raise ValueError('Select the outcome and describe the searches and findings (10–3000 characters).')
    if checked_at>datetime.utcnow()+timedelta(minutes=5) or checked_at<datetime.utcnow()-timedelta(days=7):
        raise ValueError('Enter the actual check time within the last seven days, not a future date.')
    uploads=[f for f in uploads if f.filename]
    if not 1<=len(uploads)<=5:raise ValueError('Upload 1–5 PNG or JPEG screenshots of the FIC results.')
    images=[]
    for upload in uploads:
        data=upload.read(10*1024*1024+1)
        if len(data)>10*1024*1024:raise ValueError('Each screenshot must be 10 MB or smaller.')
        try:
            im=Image.open(BytesIO(data))
            if im.format not in {'PNG','JPEG'} or im.width*im.height>25000000:raise ValueError('Use PNG or JPEG screenshots up to 25 megapixels.')
            im.load();out=BytesIO();im.convert('RGB').save(out,format='PNG');images.append(out.getvalue())
        except (UnidentifiedImageError,OSError,Image.DecompressionBombError) as exc:
            raise ValueError('One of the files is not a valid screenshot.') from exc
    row=ApplicationScreening(application_id=a.id,identity_hash=fingerprint(a),status='Employee checked' if outcome=='no_match' else 'Needs review',checked_at=checked_at,
        results_json=json.dumps([{'search':'employee','query':a.first_names+' '+a.surname,'results':[], 'outcome':outcome,'employee_id':user_id,'notes':notes,'source':FIC_URL}]),review_notes=notes)
    db.session.add(row);db.session.flush()
    paths=[]
    for i,data in enumerate(images,1):
        file=Path(application_folder(a))/f'fic-screening-{row.id}-employee-{i}.png';file.write_bytes(data);store_document(a,file);paths.append(file.name)
    row.evidence_json=json.dumps(paths)
    db.session.add(AuditLog(user_id=user_id,action='FIC_EMPLOYEE_CHECK',entity_type='ClientApplication',entity_id=str(a.id),details=f'Screening {row.id}: {row.status}; screenshot evidence saved.'))
    db.session.flush()
    return row
