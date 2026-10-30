"""Private, read-only export of the records removed by a clean-start reset."""
import base64
import hashlib
import io
import json
import zipfile
from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select, text
from app import db
from app.services import data_reset as records


OPERATIONAL_MODELS = (
    records.ApplicationFilingDelivery, records.ApplicationJourney, records.ApplicationScreening,
    records.ApplicationCDD, records.ApplicationMarketingConsent, records.ApplicationMemberAddition,
    records.ApplicationMemberCountReview, records.ClientStoredFile, records.DocumentSignature,
    records.ClientFicaDocument, records.ApplicationSignature, records.ComplianceReview,
    records.TelesalesScriptSession, records.ClientCommunication, records.SupportingDocumentReminder,
    records.ClientApplication, records.WhatsAppMessage, records.WhatsAppConversation, records.WhatsAppContact,
    records.WhatsAppWebhookEvent, records.CommunicationFollowUp, records.CommunicationEvent,
    records.CampaignRecipient, records.WhatsAppProviderJob, records.WhatsAppProviderLog,
    records.WhatsAppAuditEvent, records.CallSummary, records.ComplianceFlag, records.CallRecording,
    records.RecoveryCallLog, records.ContactCommunicationPreference, records.HistoricalMemberCover,
    records.MarketingScreening, records.LapsedPolicy, records.AuditLog, records.AgentNotification,
    records.ContactSuppression, records.CommunicationCampaign,
)


def _encode(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'type': 'bytes', 'base64': base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value, (datetime, date)):
        return {'type': 'datetime' if isinstance(value, datetime) else 'date', 'value': value.isoformat()}
    if isinstance(value, Decimal):
        return {'type': 'decimal', 'value': str(value)}
    raise TypeError('Unsupported backup value: ' + type(value).__name__)


def build_reset_backup(*, connection=None):
    own_connection = connection is None
    if own_connection:
        connection = db.engine.connect()
        if db.engine.dialect.name == 'postgresql':
            connection = connection.execution_options(isolation_level='REPEATABLE READ')
    tables = {}
    with connection if own_connection else nullcontext(connection):
        if own_connection and db.engine.dialect.name == 'postgresql':
            connection.execute(text('SET TRANSACTION READ ONLY'))
        for model in OPERATIONAL_MODELS:
            table = model.__table__
            tables[table.name] = [dict(row) for row in connection.execute(
                select(table).order_by(*table.primary_key.columns)).mappings()]
    payload = {'format': 'telesales-operational-backup-v1', 'tables': tables}
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_encode, separators=(',', ':')).encode('utf-8')
    # Provider template checks can update retained campaign metadata in the background.
    # Only client data and campaign activity determine whether a reset backup is current.
    fingerprint_tables = dict(tables)
    fingerprint_tables['communication_campaigns'] = [
        {key: row[key] for key in ('id', 'sent_at', 'scheduled_at', 'queue_status', 'qr_scan_count', 'archived_at', 'deleted_at')}
        for row in tables['communication_campaigns']]
    fingerprint = hashlib.sha256(json.dumps(fingerprint_tables, sort_keys=True, default=_encode,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    manifest = {'created_at_utc': datetime.utcnow().isoformat(), 'sha256_records': hashlib.sha256(data).hexdigest(),
        'rows': {name: len(rows) for name, rows in tables.items()},
        'retained_configuration': ['users', 'roles', 'products and member rules', 'companies', 'settings', 'WhatsApp templates', 'company documents']}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('records.json', data)
        archive.writestr('manifest.json', json.dumps(manifest, indent=2))
        archive.writestr('README.txt', 'Private operational backup, including stored document and recording bytes.\n'
            'records.json preserves table names, primary keys, references and typed values.\n'
            'Configuration stays in the system and is not removed by the reset.\n'
            'Restoration requires an administrator; this is not an Excel import file.\n')
    output.seek(0)
    return output, fingerprint
