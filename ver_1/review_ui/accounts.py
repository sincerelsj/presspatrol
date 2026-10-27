"""Local accounts and opaque, expiring server-side sessions."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from storage import ReviewError, now

def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ReviewError('비밀번호는 12~256자로 입력하세요.')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return salt + ':' + digest

def verify_password(password, encoded):
    salt, expected = encoded.split(':')
    digest = hashlib.pbkdf2_hmac('sha256', str(password).encode(), bytes.fromhex(salt), 600000).hex()
    return hmac.compare_digest(expected, digest)

class Accounts:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
              display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
              role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_attempts (key TEXT NOT NULL, ts REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS login_rate ON login_attempts(key,ts);
            CREATE TABLE IF NOT EXISTS imports (
              id INTEGER PRIMARY KEY AUTOINCREMENT, source_file TEXT NOT NULL, sha256 TEXT NOT NULL,
              row_count INTEGER NOT NULL, inserted INTEGER NOT NULL, kept INTEGER NOT NULL,
              conflicts INTEGER NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    def create(self, username, display_name, password, role='reviewer', actor=None):
        if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,40}', username):
            raise ReviewError('아이디는 영문·숫자·._- 3~40자로 입력하세요.')
        if role not in ('admin', 'reviewer'):
            raise ReviewError('잘못된 계정 역할입니다.')
        hashed = password_hash(password)
        try:
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                cur = c.execute('INSERT INTO users(username,display_name,password_hash,role,created_at) VALUES(?,?,?,?,?)',
                                (username, str(display_name or username)[:80], hashed, role, now()))
                uid = cur.lastrowid
                if actor:
                    self.store.for_user(actor).event(c, '*', 'user:create', {}, {'user_id': uid, 'username': username, 'role': role})
        except sqlite3.IntegrityError:
            raise ReviewError('이미 사용 중인 아이디입니다.', 409)
        return {'id': uid, 'username': username, 'role': role}

    def login(self, username, password, peer):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
            raise ReviewError('아이디 또는 비밀번호가 올바르지 않습니다.', 401)
        stamp = time.time()
        key = hashlib.sha256((peer + ':' + username.lower()).encode()).hexdigest()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM login_attempts WHERE ts<?', (stamp - 600,))
            if c.execute('SELECT count(*) FROM login_attempts WHERE key=? AND ts>?', (key, stamp - 300)).fetchone()[0] >= 5:
                raise ReviewError('로그인 시도가 많습니다. 5분 후 다시 시도하세요.', 429)
            c.execute('INSERT INTO login_attempts VALUES (?,?)', (key, stamp))
            user = c.execute('SELECT * FROM users WHERE username=? AND active=1', (username,)).fetchone()
        # Constant PBKDF2 cost even for nonexistent usernames.
        dummy = '00' * 16 + ':' + '00' * 32
        valid = verify_password(password, user['password_hash'] if user else dummy)
        if not user or not valid:
            raise ReviewError('아이디 또는 비밀번호가 올바르지 않습니다.', 401)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store.connect() as c:
            c.execute('DELETE FROM login_attempts WHERE key=?', (key,))
            c.execute('DELETE FROM sessions WHERE expires<?', (stamp,))
            c.execute('INSERT INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], csrf, stamp + 43200))
        return token, self.session(token)

    def session(self, token):
        if not token:
            raise ReviewError('로그인이 필요합니다.', 401)
        with self.store.connect() as c:
            r = c.execute('''SELECT u.id,u.username,u.display_name,u.role,s.csrf FROM sessions s
              JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1''',
                          (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not r:
            raise ReviewError('로그인이 만료되었습니다.', 401)
        return dict(r)

    def logout(self, token):
        with self.store.connect() as c:
            c.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def change_password(self, user, body):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r = c.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()
            if not verify_password(str(body.get('current_password', ''))[:256], r[0]):
                raise ReviewError('현재 비밀번호가 일치하지 않습니다.')
            hashed = password_hash(body.get('new_password'))
            c.execute('UPDATE users SET password_hash=? WHERE id=?', (hashed, user['id']))
            c.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
            self.store.for_user(user).event(c, '*', 'user:password-change', {}, {'user_id': user['id']})

    def dashboard(self):
        with self.store.connect() as c:
            users = [dict(r) for r in c.execute('''SELECT u.id,u.username,u.display_name,u.role,u.active,
              count(a.media_id) assigned, sum(CASE WHEN b.state='completed' THEN 1 ELSE 0 END) completed
              FROM users u LEFT JOIN assignments a ON a.user_id=u.id LEFT JOIN businesses b ON b.id=a.media_id
              GROUP BY u.id ORDER BY u.id''')]
            total = c.execute('SELECT count(*) FROM businesses').fetchone()[0]
            free = c.execute('SELECT count(*) FROM businesses WHERE id NOT IN (SELECT media_id FROM assignments)').fetchone()[0]
            imports = [dict(r) for r in c.execute('SELECT * FROM imports ORDER BY id DESC LIMIT 20')]
        return {'users': users, 'total': total, 'unassigned': free, 'imports': imports}

    def assign(self, user, body):
        uid, count = body.get('user_id'), body.get('count', 1000)
        if type(uid) is not int or type(count) is not int or not 1 <= count <= 1000:
            raise ReviewError('검토자와 배정 건수(1~1,000)를 확인하세요.')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            target = c.execute("SELECT * FROM users WHERE id=? AND role='reviewer' AND active=1", (uid,)).fetchone()
            if not target:
                raise ReviewError('활성 검토자 계정만 배정할 수 있습니다.')
            current = c.execute('SELECT count(*) FROM assignments WHERE user_id=?', (uid,)).fetchone()[0]
            if current + count > 1000:
                raise ReviewError(f'검토자당 최대 1,000건입니다. 현재 {current}건 배정되어 있습니다.')
            rows = c.execute('SELECT id FROM businesses WHERE id NOT IN (SELECT media_id FROM assignments) ORDER BY ordinal LIMIT ?', (count,)).fetchall()
            if len(rows) < count:
                raise ReviewError('미배정 사업체가 부족합니다.')
            c.executemany('INSERT INTO assignments VALUES(?,?,?)', [(r['id'], uid, now()) for r in rows])
            c.execute('DELETE FROM user_setup WHERE user_id=?', (uid,))
            self.store.for_user(user).event(c, '*', 'assignment:create', {}, {'user_id': uid, 'ids': [r['id'] for r in rows]})
        return {'assigned': count, 'user_id': uid}
