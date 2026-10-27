"""Dependency-free local/internal review demo server (Python 3.10+)."""
import argparse
import json
import mimetypes
import os
import hmac
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from storage import Store, ReviewError
from accounts import Accounts
from raw_data import RawData

BASE = Path(__file__).resolve().parent

def make_handler(store, source_root=None):
    root = Path(source_root or store.metadata().get('source_root', BASE.parent / 'run_full')).resolve()
    accounts, raw = Accounts(store), RawData(store, root)

    class Handler(BaseHTTPRequestHandler):
        def token(self):
            cookies = SimpleCookie()
            try: cookies.load(self.headers.get('Cookie', ''))
            except Exception: return ''
            return cookies['review_session'].value if 'review_session' in cookies else ''

        def identity(self, admin=False, write=False):
            user = accounts.session(self.token())
            if admin and user['role'] != 'admin':
                raise ReviewError('관리자 권한이 필요합니다.', 403)
            if write and not hmac.compare_digest(user['csrf'], self.headers.get('X-CSRF-Token', '')):
                raise ReviewError('요청 검증에 실패했습니다. 새로고침 후 다시 시도하세요.', 403)
            return user

        def cookie(self, token, expired=False):
            value = 'review_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + ('0' if expired else '43200')
            if os.environ.get('REVIEW_COOKIE_SECURE') == '1': value += '; Secure'
            return value

        def send(self, data, status=200, kind='application/json; charset=utf-8', attachment=None):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if getattr(self, 'set_cookie', None):self.send_header('Set-Cookie', self.set_cookie)
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'self'")
            if attachment:
                self.send_header('Content-Disposition', 'attachment; filename="' + attachment + '"')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                url = urlparse(self.path)
                path = url.path
                if path == '/api/health':
                    return self.send({'ok': True})
                if path == '/api/me':
                    return self.send(self.identity())
                user = self.identity(admin=path.startswith('/api/admin')) if path.startswith(('/api/', '/evidence/')) else None
                scoped = store.for_user(user) if user else store
                if path == '/api/training' or path.startswith('/api/training/'):
                    training = BASE / 'resources' / 'training'
                    if path == '/api/training':
                        file, kind = training / 'manifest.json', 'application/json; charset=utf-8'
                    elif path == '/api/training/pdf':
                        file, kind = training / 'source.pdf', 'application/pdf'
                    elif re.fullmatch(r'/api/training/page/(?:[1-9]|10)\.png', path):
                        file, kind = training / path.rsplit('/', 1)[-1], 'image/png'
                    else:
                        raise ReviewError('학습자료 페이지를 찾을 수 없습니다.', 404)
                    if not file.is_file():
                        raise ReviewError('학습자료가 없습니다. resources/training 폴더를 확인하세요.', 404)
                    return self.send(file.read_bytes(), kind=kind)
                if path == '/api/admin/dashboard':
                    return self.send({**accounts.dashboard(), 'files': raw.files()})
                if path == '/api/admin/raw/preview':
                    return self.send(raw.preview(parse_qs(url.query).get('file', [''])[0]))
                if path == '/api/assignment':
                    listing = scoped.listing()
                    prototype = BASE / 'prototype.json'
                    if parse_qs(url.query).get('view') == ['prototype'] and prototype.is_file():
                        cases = json.loads(prototype.read_text(encoding='utf-8'))['cases']
                        by_id = {r['id']: r for r in listing['businesses']}
                        listing['businesses'] = [{**by_id[r['id']], 'case_label': r['label']} for r in cases if r['id'] in by_id]
                        listing['meta']['count'] = len(listing['businesses'])
                        listing['meta']['prototype'] = True
                    return self.send(listing)
                if path.startswith('/api/business/'):
                    return self.send(raw.enrich(scoped.detail(path.rsplit('/', 1)[-1])))
                if path == '/api/export':
                    complete = parse_qs(url.query).get('mode') == ['complete']
                    return self.send(scoped.export(complete), kind='text/csv; charset=utf-8', attachment='review-complete.csv' if complete else 'review-draft.csv')
                if path == '/api/jobs/export':
                    with store.connect() as c:
                        where, params = scoped.scope()
                        jobs = [dict(r) for r in c.execute('SELECT * FROM jobs'+where.replace('WHERE id IN','WHERE media_id IN')+' ORDER BY id',params)]
                    return self.send(json.dumps({'reviewer': user['username'], 'jobs': jobs}, ensure_ascii=False, indent=2).encode('utf-8'), attachment='reprocess-queue.json')
                if path.startswith('/evidence/'):
                    parts = path.split('/')
                    if len(parts) != 4:
                        raise ReviewError('잘못된 증빙 경로입니다.', 404)
                    rid, suffix = parts[2:]
                    detail = scoped.detail(rid)
                    if suffix not in detail['source']['assets']:
                        raise ReviewError('배정된 증빙이 아닙니다.', 404)
                    file = (root / 'shots' / f'{rid}_{suffix}').resolve()
                    if file.parent != (root / 'shots').resolve() or not file.is_file():
                        raise ReviewError('증빙 파일이 없습니다. 서버의 --source 경로를 확인하세요.', 404)
                    return self.send(file.read_bytes(), kind='image/png')
                files = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                         '/login': 'login.html', '/login.js': 'login.js', '/admin': 'admin.html', '/admin.js': 'admin.js', '/admin-style.css':'admin-style.css', '/prototype.css':'prototype.css'}
                if path not in files:
                    raise ReviewError('페이지를 찾을 수 없습니다.', 404)
                file = BASE / 'web' / files[path]
                return self.send(file.read_bytes(), kind=(mimetypes.guess_type(file)[0] or 'text/plain') + '; charset=utf-8')
            except ReviewError as e:
                self.send({'error': str(e)}, e.status)
            except Exception:
                import traceback
                traceback.print_exc()
                self.send({'error': '서버 오류가 발생했습니다. 서버 로그를 확인하세요.'}, 500)

        def do_POST(self):
            try:
                # Same-origin JSON only. A reverse proxy must preserve the Host header.
                origin = self.headers.get('Origin')
                if origin and urlparse(origin).netloc != self.headers.get('Host'):
                    raise ReviewError('다른 출처의 저장 요청은 허용하지 않습니다.', 403)
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ReviewError('JSON 요청만 허용합니다.', 415)
                length = int(self.headers.get('Content-Length', 0))
                if length <= 0 or length > 256_000:
                    raise ReviewError('요청 크기가 올바르지 않습니다.', 413)
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise ReviewError('JSON 형식이 올바르지 않습니다.')
                if not isinstance(body, dict):
                    raise ReviewError('JSON 객체가 필요합니다.')
                path = urlparse(self.path).path
                if path == '/api/login':
                    token, user = accounts.login(body.get('username'),body.get('password'),self.client_address[0])
                    self.set_cookie = self.cookie(token)
                    return self.send(user)
                user = self.identity(admin=path.startswith('/api/admin'), write=True)
                scoped = store.for_user(user)
                if path == '/api/logout':
                    accounts.logout(self.token());self.set_cookie=self.cookie('',True)
                    return self.send({'ok':True})
                if path == '/api/password':
                    accounts.change_password(user,body);self.set_cookie=self.cookie('',True)
                    return self.send({'ok':True})
                if path == '/api/admin/users':
                    return self.send(accounts.create(body.get('username'),body.get('display_name'),body.get('password'),body.get('role','reviewer'),user))
                if path == '/api/admin/assign':
                    return self.send(accounts.assign(user,body))
                if path == '/api/admin/raw/import':
                    return self.send(raw.import_file(user,body.get('file'),body.get('sha256')))
                if path == '/api/setup':
                    return self.send(scoped.setup(body))
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[:2] == ['api', 'business']:
                    if parts[3] == 'review':
                        return self.send(raw.enrich(scoped.save(parts[2], body)))
                    if parts[3] == 'jobs':
                        return self.send(raw.enrich(scoped.request_job(parts[2], body)))
                raise ReviewError('요청 경로가 없습니다.', 404)
            except ReviewError as e:
                self.send({'error': str(e)}, e.status)
            except Exception:
                import traceback
                traceback.print_exc()
                self.send({'error': '저장하지 못했습니다. 서버 로그를 확인하세요.'}, 500)

    return Handler

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('REVIEW_DATA_DIR', BASE / 'data')))
    parser.add_argument('--source', type=Path, default=os.environ.get('REVIEW_SOURCE_ROOT'))
    args = parser.parse_args()
    store = Store(args.data_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, args.source))
    print(f'Review UI: http://{args.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
