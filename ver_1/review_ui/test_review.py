import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from storage import Store, ReviewError, FIELDS
from server import make_handler

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).resolve().parent / 'data_test'
        self.test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.test_root)
        self.store = Store(self.temp.name)
        source = {'name': '테스트 매체', 'url': 'https://example.invalid', 'decision': '준수',
                  'operation': '있음', 'flagged': False, 'reason': '', 'missing_evidence': '',
                  'assets': ['footer.png'], 'search_decision': ''}
        with self.store.connect() as c:
            c.execute('INSERT INTO meta VALUES (?,?)', ('reviewer', '"검토자 01"'))
            c.execute('INSERT INTO businesses(id,ordinal,source) VALUES(?,?,?)', ('1', 1, json.dumps(source)))
        self.store.setup({'confirmed': True})

    def tearDown(self):
        assert Path(self.temp.name).resolve().parent == self.test_root
        self.temp.cleanup()

    def save(self, gate, data=None, action='approve', note='확인한 원본 근거'):
        v = self.store.detail('1')['version']
        return self.store.save('1', {'version': v, 'gate': gate, 'data': {'checked': True, **(data or {})}, 'action': action, 'note': note})

    def prefix(self):
        self.save(1, {'url': 'https://example.invalid'})
        self.save(2, {'quality': 'usable'})
        self.save(3, {'operation': '있음'})
        self.save(4)

    def fields(self):
        return {f: {'decision': 'present', 'value': '원문 값', 'method': 'direct', 'evidence': '하단 원문 확인'} for f in FIELDS}

    def test_persistence_conflict_and_invalidation(self):
        self.prefix()
        with self.assertRaises(ReviewError) as caught:
            self.store.save('1', {'version': 0, 'gate': 1, 'action': 'approve', 'data': {}})
        self.assertEqual(caught.exception.status, 409)
        self.save(1, {'url': 'https://example.invalid'})
        fresh = Store(self.temp.name).detail('1')
        self.assertEqual(list(fresh['review']['gates']), ['1'])
        self.assertEqual(len(fresh['events']), 5)

    def test_gate_order_and_article_exception(self):
        with self.assertRaises(ReviewError):
            self.save(5)
        self.prefix()
        fields = self.fields()
        fields['등록연월일'].update(decision='absent', method='absent')
        with self.assertRaises(ReviewError):
            self.save(5, {'fields': fields})
        fields['발행연월일']['method'] = 'article'
        self.save(5, {'fields': fields})
        self.assertEqual(self.store.detail('1')['review']['gates']['5']['status'], 'approved')

    def test_export_completion_and_atomic_audit(self):
        with self.assertRaises(ReviewError):
            self.store.export(True)
        self.prefix()
        self.save(5, {'fields': self.fields()})
        self.save(6, {'comparisons': {f: '일치' for f in ['등록번호', '발행인', '편집인', '발행소 주소', '발행소 전화번호']}})
        self.save(7)
        self.save(8)
        self.assertIn('완료본', self.store.export(True).decode('utf-8-sig'))
        self.assertEqual(self.store.detail('1')['state'], 'completed')
        self.assertEqual(len(self.store.detail('1')['events']), 8)

    def test_missing_search_cannot_be_approved(self):
        self.save(1, {'url': 'https://example.invalid'})
        self.save(2, {'quality': 'failure'})
        self.save(3, {'operation': '없음'})
        with self.assertRaises(ReviewError):
            self.save(4)
        self.save(4, action='hold')
        self.assertEqual(self.store.detail('1')['state'], 'held')

    def test_job_invalidates_and_blocks_approval(self):
        self.prefix()
        d = self.store.detail('1')
        self.store.request_job('1', {'version': d['version'], 'stage': 'capture', 'reason': '하단 잘림'})
        self.assertEqual(list(self.store.detail('1')['review']['gates']), ['1'])
        with self.assertRaises(ReviewError):
            self.save(2, {'quality': 'usable'})
        self.assertEqual(self.store.listing()['jobs'][0]['status'], 'queued')

    def test_http_validation_and_path_boundary(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store))
        thread = threading.Thread(target=http.serve_forever, daemon=True);thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        try:
            self.assertEqual(json.load(urlopen(base + '/api/health')), {'ok': True})
            self.assertIn('점검 작업실', urlopen(base).read().decode())
            for path in ['/evidence/2/footer.png', '/evidence/1/../../server.py', '/server.py']:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base + path)
                self.assertEqual(caught.exception.code, 401 if path.startswith('/evidence/') else 404)
            req = Request(base + '/api/setup', data=b'{}', headers={'Content-Type': 'application/json', 'Origin': 'https://wrong.invalid'})
            with self.assertRaises(HTTPError) as caught:
                urlopen(req)
            self.assertEqual(caught.exception.code, 403)
        finally:
            http.shutdown();http.server_close();thread.join()

if __name__ == '__main__':
    unittest.main()
