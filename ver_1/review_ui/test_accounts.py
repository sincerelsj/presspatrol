import csv
import io
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from accounts import Accounts
from storage import Store, ReviewError
from raw_data import RawData
from server import make_handler

class AccountTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).resolve().parent/'data_test';self.root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=self.root);self.path=Path(self.temp.name)
        self.store=Store(self.path/'db');self.accounts=Accounts(self.store)
        self.admin=self.accounts.create('admin','관리자','Example-password-123','admin')
        self.a=self.accounts.create('reviewer_a','검토자 A','Example-password-456')
        self.b=self.accounts.create('reviewer_b','검토자 B','Example-password-789')
        self.source=self.path/'raw';(self.source/'outputs').mkdir(parents=True)
        self.file=self.source/'outputs/verify_2026-09-03.csv'
        self.write_raw('매체 A')
        self.raw=RawData(self.store,self.source)

    def write_raw(self,name):
        with self.file.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['언론사_id','제호_등록','메인_url','홈페이지_운영현황','준수여부_LLM'])
            w.writeheader()
            for rid,title in [('1',name),('2','매체 B')]:w.writerow({'언론사_id':rid,'제호_등록':title,'메인_url':'https://example.invalid','홈페이지_운영현황':'있음','준수여부_LLM':'준수'})

    def tearDown(self):
        assert self.path.resolve().parent==self.root
        self.temp.cleanup()

    def import_rows(self):
        preview=self.raw.preview(self.file.name)
        return self.raw.import_file(self.admin,self.file.name,preview['sha256'])

    def test_login_hash_session_logout(self):
        token,user=self.accounts.login('admin','Example-password-123','local')
        self.assertEqual(user['role'],'admin')
        with self.store.connect() as c:
            stored=c.execute('SELECT password_hash FROM users WHERE username=?',('admin',)).fetchone()[0]
            self.assertNotIn('Example-password',stored)
        self.accounts.logout(token)
        with self.assertRaises(ReviewError):self.accounts.session(token)

    def test_scope_and_assignment_transaction(self):
        self.import_rows();self.accounts.assign(self.admin,{'user_id':self.a['id'],'count':1})
        self.accounts.assign(self.admin,{'user_id':self.b['id'],'count':1})
        a=self.store.for_user({**self.a,'display_name':'A'});b=self.store.for_user({**self.b,'display_name':'B'})
        self.assertEqual([r['id'] for r in a.listing()['businesses']],['1'])
        self.assertEqual([r['id'] for r in b.listing()['businesses']],['2'])
        self.assertNotIn('매체 B',a.export().decode('utf-8-sig'))
        with self.assertRaises(ReviewError):a.detail('2')
        with self.assertRaises(ReviewError):a.request_job('2',{'version':0,'stage':'search','reason':'test'})
        with self.assertRaises(ReviewError):a.save('2',{'version':0,'gate':1,'action':'draft','data':{}})
        with self.assertRaises(ReviewError):self.accounts.assign(self.admin,{'user_id':self.a['id'],'count':1})
        a.setup({'confirmed':True})
        self.assertFalse(b.listing()['meta']['setup']['confirmed'])

    def test_raw_repeat_changed_conflict_and_hash_check(self):
        self.assertEqual(self.import_rows()['inserted'],2)
        self.assertEqual(self.import_rows()['kept'],2)
        previous=self.raw.preview(self.file.name)['sha256']
        self.write_raw('이름 변경')
        with self.assertRaises(ReviewError):self.raw.import_file(self.admin,self.file.name,previous)
        result=self.import_rows()
        self.assertEqual(result['conflicts'],1)
        self.assertEqual(self.store.detail('1')['source']['name'],'매체 A')
        with self.assertRaises(ReviewError):self.raw.preview('../../other.csv')

    def test_http_login_csrf_admin_and_evidence_permissions(self):
        # Self-contained transport fixtures; never depend on supplied training assets.
        training = self.path / 'resources' / 'training'
        training.mkdir(parents=True)
        (training / 'manifest.json').write_text(json.dumps({'pages': [
            {'printed_page': 14 + i} for i in range(10)
        ]}), encoding='utf-8')
        (training / 'source.pdf').write_bytes(b'%PDF-1.4\n% transport-test fixture\n')
        (training / '10.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        resource_patch = patch('server.BASE', self.path)
        resource_patch.start()
        self.addCleanup(resource_patch.stop)
        self.import_rows();self.accounts.assign(self.admin,{'user_id':self.a['id'],'count':1})
        http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,self.source))
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{http.server_port}'
        def req(path,body=None,cookie='',csrf=''):
            return urlopen(Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json','Cookie':cookie,'X-CSRF-Token':csrf}))
        try:
            with self.assertRaises(HTTPError) as e:req('/api/assignment')
            self.assertEqual(e.exception.code,401)
            for path in ['/api/training', '/api/training/pdf', '/api/training/page/1.png']:
                with self.assertRaises(HTTPError) as e:req(path)
                self.assertEqual(e.exception.code,401)
            login=req('/api/login',{'username':'reviewer_a','password':'Example-password-456'})
            cookie=login.headers['Set-Cookie'].split(';')[0];user=json.load(login)
            self.assertIn('HttpOnly',login.headers['Set-Cookie'])
            training=json.load(req('/api/training',cookie=cookie))
            self.assertEqual(len(training['pages']),10)
            self.assertEqual(training['pages'][0]['printed_page'],14)
            pdf=req('/api/training/pdf',cookie=cookie)
            self.assertEqual(pdf.headers['Content-Type'],'application/pdf')
            self.assertTrue(pdf.read().startswith(b'%PDF-'))
            self.assertTrue(req('/api/training/page/10.png',cookie=cookie).read().startswith(b'\x89PNG\r\n\x1a\n'))
            for path in ['/api/training/page/11.png', '/api/training/page/../source.pdf']:
                with self.assertRaises(HTTPError) as e:req(path,cookie=cookie)
                self.assertEqual(e.exception.code,404)
            self.assertEqual(len(json.load(req('/api/assignment',cookie=cookie))['businesses']),1)
            for path in ['/api/admin/dashboard','/api/business/2','/evidence/2/footer.png']:
                with self.assertRaises(HTTPError):req(path,cookie=cookie)
            with self.assertRaises(HTTPError) as e:req('/api/setup',{'confirmed':True},cookie)
            self.assertEqual(e.exception.code,403)
            self.assertTrue(json.load(req('/api/setup',{'confirmed':True},cookie,user['csrf']))['confirmed'])
            with self.assertRaises(HTTPError):req('/api/admin/assign',{'user_id':self.a['id'],'count':1},cookie,user['csrf'])
            req('/api/logout',{},cookie,user['csrf'])
            with self.assertRaises(HTTPError):req('/api/me',cookie=cookie)
            login=req('/api/login',{'username':'admin','password':'Example-password-123'})
            cookie=login.headers['Set-Cookie'].split(';')[0];user=json.load(login)
            preview=json.load(req('/api/admin/raw/preview?file=verify_2026-09-03.csv',cookie=cookie))
            imported=json.load(req('/api/admin/raw/import',{'file':preview['file'],'sha256':preview['sha256']},cookie,user['csrf']))
            self.assertEqual(imported['kept'],2)
            self.assertEqual(json.load(req('/api/admin/dashboard',cookie=cookie))['total'],2)
            req('/api/password',{'current_password':'Example-password-123','new_password':'Changed-password-123'},cookie,user['csrf'])
            with self.assertRaises(HTTPError):req('/api/me',cookie=cookie)
        finally:http.shutdown();http.server_close();thread.join()

if __name__=='__main__':unittest.main()
