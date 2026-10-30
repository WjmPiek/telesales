import io
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from PIL import Image
from tests import test_analytics_security as fixtures
from app import db
from app.models import ClientApplication, PolicyProduct, PolicyProductRule, ApplicationJourney, ClientFicaDocument
from app.services.document_status_service import required_fica_documents, document_summary


class AllMemberDocumentsTests(unittest.TestCase):
    setUpBase = fixtures.AnalyticsSecurityTests.setUp
    tearDown = fixtures.AnalyticsSecurityTests.tearDown

    def setUp(self):
        self.setUpBase()
        product = PolicyProduct(product_name='Member 1 + 5 R20 000', plan_name='Member+5', active=True)
        db.session.add(product)
        db.session.flush()
        # Reproduce the live mismatch: product rules still have the family default.
        db.session.add(PolicyProductRule(product_id=product.id, plan_type='family'))
        self.members = [
            {'full_name':'Example Spouse', 'relationship':'Spouse', 'id_or_dob':'1985-01-01'},
            {'full_name':'Example Child One', 'relationship':'Child', 'id_or_dob':'2010-03-04'},
            {'full_name':'Example Child Two', 'relationship':'Child', 'id_or_dob':'2015-06-07'},
        ]
        record = ClientApplication(application_ref='ALL-MEMBER-DOCS', product=product, form_template='member_product',
            first_names='Example', surname='Principal', id_number='8001015009087', status='Signed',
            sign_token='all-member-docs-token', signed_at=datetime.utcnow(), branch='A',
            agent_id=self.user_ids['admin'], product_dependents_json=json.dumps(self.members))
        db.session.add(record)
        db.session.flush()
        db.session.add(ApplicationJourney(application_id=record.id, signed_bundle_at=datetime.utcnow()))
        db.session.commit()
        self.aid = record.id
        self.public = self.app.test_client()
        self.link = '/sign/all-member-docs-token/supporting-documents'

    @property
    def record(self):
        return db.session.get(ClientApplication, self.aid)

    def keys(self):
        return [row['key'] for row in required_fica_documents(self.record)]

    @staticmethod
    def photo(index):
        stream = io.BytesIO()
        Image.new('RGB', (120, 120), (index * 30, 50, 70)).save(stream, format='PNG')
        stream.seek(0)
        return stream

    def unlock(self):
        self.assertEqual(self.public.post(self.link, data={'action':'unlock', 'id_number':'8001015009087'}).status_code, 302)

    def test_member_product_with_old_family_rules_lists_every_member(self):
        self.assertEqual(self.keys(), ['id_copy','member_1_id_copy','member_2_id_copy','member_3_id_copy','proof_of_address'])
        self.unlock()
        page = self.public.get(self.link)
        for member in self.members:
            self.assertIn(member['full_name'].encode(), page.data)
        summary_keys = [row['key'] for row in document_summary(self.record)['missing']]
        for key in self.keys():
            self.assertIn(key, summary_keys)

    def test_family_mirrors_do_not_request_each_person_twice(self):
        a = self.record
        a.spouse_first_names, a.spouse_surname, a.spouse_date_of_birth = 'Example', 'Spouse', '01/01/1985'
        a.dependents_json = json.dumps(self.members[1:])
        a.form_template = 'family'
        db.session.commit()
        self.assertEqual(self.keys(), ['id_copy','spouse_id_copy','child_1_id_copy','child_2_id_copy','proof_of_address'])

    def test_mixed_saved_lists_keep_all_people_and_original_upload_keys(self):
        self.record.spouse_first_names = 'Another Spouse'
        self.record.dependents_json = json.dumps([{'full_name':'Legacy Child','id_number':'2012-01-01'}])
        self.record.extended_family_json = json.dumps([{'full_name':'Example Parent','date_of_birth':'1960-01-01'}])
        db.session.commit()
        self.assertEqual(self.keys(), ['id_copy','spouse_id_copy','child_1_id_copy','extended_1_id_copy',
                                      'member_1_id_copy','member_2_id_copy','member_3_id_copy','proof_of_address'])

    def test_portal_requires_every_member_file_before_completion(self):
        self.unlock()
        keys = self.keys()
        for index, key in enumerate(keys[:-1], 1):
            response = self.public.post(self.link, data={'action':'upload', 'document_type':key,
                'file':(self.photo(index), f'{key}.png')}, content_type='multipart/form-data')
            self.assertEqual(response.status_code, 302)
        incomplete = self.public.post(self.link, data={'action':'complete'})
        self.assertIn(b'Please upload every required document', incomplete.data)
        self.assertEqual(ClientFicaDocument.query.filter_by(application_id=self.aid).count(), len(keys)-1)
        self.public.post(self.link, data={'action':'upload', 'document_type':keys[-1],
            'file':(self.photo(len(keys)), 'proof.png')}, content_type='multipart/form-data')
        completed = self.public.post(self.link, data={'action':'complete'})
        self.assertIn(b'Supporting documents received', completed.data)
        self.assertEqual(self.record.status, 'FICA Review')

    def test_signed_application_replacement_page_accepts_member_files(self):
        self.unlock()
        path = '/online-application/all-member-docs-token'
        page = self.public.get(path)
        self.assertIn(b'Example Spouse', page.data)
        self.assertIn(b'Example Child Two', page.data)
        with self.public.session_transaction() as s:
            nonce = s[f'questionnaire_nonce_{self.aid}']
        with patch('app.routes.signing._save_upload') as upload:
            response = self.public.post(path, data={'action':'replace', 'nonce':nonce,
                'member_2_id_copy':(self.photo(1), 'child-id.png')}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(upload.call_args.args[1], 'member_2_id_copy')
        self.assertEqual(self.record.status, 'QA Pending')


if __name__ == '__main__':
    unittest.main()
