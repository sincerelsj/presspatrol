"""Review storage. Automatic findings remain immutable; every user mutation is audited."""
import csv
import hashlib
import io
import json
import sqlite3
import copy
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

FIELDS = ['제호', '등록번호', '등록연월일', '발행인', '편집인', '발행연월일', '발행소 주소', '발행소 전화번호']
GATES = ['등록부·URL', '수집·증빙', '홈페이지 유무', '검색·주소', '게재사항 8항목', '등록정보 대조', '증빙·예외', '최종 승인']

def now():
    return datetime.now(timezone.utc).isoformat()

class ReviewError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

class Store:
    def __init__(self, data_dir):
        self.user = None
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'reviews.sqlite3'
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS businesses (
              id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL, source TEXT NOT NULL,
              review TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'pending', updated_at TEXT);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, media_id TEXT NOT NULL,
              actor TEXT NOT NULL, action TEXT NOT NULL, before_json TEXT NOT NULL,
              after_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, media_id TEXT NOT NULL,
              stage TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
              source_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            ''')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS assignments (media_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, assigned_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS user_setup (user_id INTEGER PRIMARY KEY, confirmed_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS assignment_owner ON assignments(user_id);
            ''')

    def for_user(self, user):
        scoped = copy.copy(self)
        scoped.user = user
        return scoped

    def scope(self):
        if self.user and self.user['role'] != 'admin':
            return ' WHERE id IN (SELECT media_id FROM assignments WHERE user_id=?)', (self.user['id'],)
        return '', ()

    def authorize(self, c, media_id):
        if self.user and self.user['role'] != 'admin':
            if not c.execute('SELECT 1 FROM assignments WHERE media_id=? AND user_id=?', (media_id, self.user['id'])).fetchone():
                raise ReviewError('배정된 사업체가 아닙니다.', 404)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        try:
            with c:
                yield c
        finally:
            c.close()

    def metadata(self, c=None):
        if c is None:
            with self.connect() as conn:
                return self.metadata(conn)
        return {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM meta')}

    def setup(self, body):
        if body.get('confirmed') is not True:
            raise ReviewError('기준·대상·저장 위치 확인란을 선택하세요.')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = self.metadata(c).get('setup', {})
            result = {'confirmed': True, 'confirmed_at': now()}
            if self.user:
                c.execute('INSERT OR REPLACE INTO user_setup VALUES (?,?)', (self.user['id'], now()))
            else:
                c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('setup', json.dumps(result)))
            self.event(c, '*', 'setup', old, result)
        return result

    def event(self, c, media_id, action, before, after):
        actor = self.user['username'] if self.user else self.metadata(c).get('reviewer', 'reviewer-001')
        c.execute('INSERT INTO events(media_id,actor,action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',
                  (media_id, actor, action, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), now()))

    def listing(self):
        with self.connect() as c:
            where, params = self.scope()
            rows = c.execute('SELECT * FROM businesses' + where + ' ORDER BY ordinal', params).fetchall()
            meta = self.metadata(c)
            result = []
            for r in rows:
                s, v = json.loads(r['source']), json.loads(r['review'])
                result.append({'id': r['id'], 'name': s['name'], 'url': s['url'], 'decision': s['decision'],
                               'operation': s['operation'], 'flagged': s['flagged'], 'reason': s['reason'],
                               'missing_evidence': s['missing_evidence'], 'state': r['state'],
                               'approved_gates': sum(x.get('status') == 'approved' for x in v.get('gates', {}).values()),
                               'version': r['version'], 'updated_at': r['updated_at']})
            job_where = where.replace('WHERE id IN', 'WHERE media_id IN')
            jobs = [dict(r) for r in c.execute('SELECT * FROM jobs' + job_where + ' ORDER BY id DESC LIMIT 100', params)]
            if self.user:
                meta['reviewer'] = self.user['display_name']
                meta['setup'] = {'confirmed': bool(c.execute('SELECT 1 FROM user_setup WHERE user_id=?', (self.user['id'],)).fetchone())}
                meta['count'] = len(rows)
                meta['assignment_id'] = 'user-' + str(self.user['id'])
        # Host paths are not needed by the browser.
        public = {k: v for k, v in meta.items() if k != 'source_root'}
        return {'meta': public, 'businesses': result, 'jobs': jobs}

    def detail(self, media_id):
        with self.connect() as c:
            self.authorize(c, media_id)
            r = c.execute('SELECT * FROM businesses WHERE id=?', (media_id,)).fetchone()
            if not r:
                raise ReviewError('배정된 사업체가 아닙니다.', 404)
            events = [{'seq': x['seq'], 'actor': x['actor'], 'action': x['action'], 'created_at': x['created_at'],
                       'after': json.loads(x['after_json'])} for x in c.execute(
                       'SELECT * FROM events WHERE media_id=? ORDER BY seq DESC LIMIT 30', (media_id,))]
            return {'id': media_id, 'source': json.loads(r['source']), 'review': json.loads(r['review']),
                    'version': r['version'], 'state': r['state'], 'events': events}

    def check_version(self, r, body):
        if body.get('version') != r['version']:
            raise ReviewError('다른 창에서 변경된 기록입니다. 새로고침 후 다시 확인하세요.', 409)

    def save(self, media_id, body):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.authorize(c, media_id)
            r = c.execute('SELECT * FROM businesses WHERE id=?', (media_id,)).fetchone()
            if not r:
                raise ReviewError('배정된 사업체가 아닙니다.', 404)
            self.check_version(r, body)
            ready = bool(c.execute('SELECT 1 FROM user_setup WHERE user_id=?', (self.user['id'],)).fetchone()) if self.user else self.metadata(c).get('setup', {}).get('confirmed')
            if not ready:
                raise ReviewError('먼저 준비 화면에서 검토 기준을 확인하세요.')
            source = json.loads(r['source'])
            old = json.loads(r['review'])
            review = json.loads(json.dumps(old))
            gate = body.get('gate')
            action = body.get('action')
            if type(gate) is not int or gate not in range(1, 9) or action not in ('draft', 'approve', 'hold'):
                raise ReviewError('유효하지 않은 검토 요청입니다.')
            gates = review.setdefault('gates', {})
            for n in range(1, gate):
                if gates.get(str(n), {}).get('status') != 'approved':
                    raise ReviewError(f'먼저 {n}단계를 승인하세요.')
            data = body.get('data', {})
            if not isinstance(data, dict):
                raise ReviewError('입력 형식이 올바르지 않습니다.')
            note = str(body.get('note', '')).strip()[:4000]
            if action == 'hold' and not note:
                raise ReviewError('보류 사유를 입력하세요.')
            if action == 'approve':
                if c.execute("SELECT 1 FROM jobs WHERE media_id=? AND status='queued'", (media_id,)).fetchone():
                    raise ReviewError('재처리 요청이 대기 중입니다. 결과 재반영 전에는 승인할 수 없습니다.')
                self.validate(gate, data, source, review, note)
            gates[str(gate)] = {'status': {'approve': 'approved', 'draft': 'draft', 'hold': 'held'}[action],
                                'data': data, 'note': note, 'reviewed_at': now()}
            # Earlier changes invalidate downstream approvals, including final export.
            for n in range(gate + 1, 9):
                gates.pop(str(n), None)
            state = 'completed' if gate == 8 and action == 'approve' else ('held' if action == 'hold' else 'reviewing')
            c.execute('UPDATE businesses SET review=?,version=version+1,state=?,updated_at=? WHERE id=?',
                      (json.dumps(review, ensure_ascii=False), state, now(), media_id))
            self.event(c, media_id, f'G{gate}:{action}', old, review)
        return self.detail(media_id)

    def operation(self, review, source):
        return review.get('gates', {}).get('3', {}).get('data', {}).get('operation', source['operation'])

    def validate(self, gate, data, source, review, note):
        if data.get('checked') is not True:
            raise ReviewError('자료를 확인한 뒤 확인란을 선택하세요.')
        if gate == 1 and data.get('url') != source['url']:
            raise ReviewError('주소 변경은 재처리 요청으로 새 증빙을 확보한 후 반영해야 합니다.')
        if gate == 2:
            if data.get('quality') not in ('usable', 'failure'):
                raise ReviewError('증빙 상태를 선택하세요.')
            if data['quality'] == 'failure' and not note:
                raise ReviewError('수집 실패 증거와 사유를 기록하세요.')
            if not source['assets'] and not note:
                raise ReviewError('캡처가 없으면 확인한 오류 증거를 사유에 기록하세요.')
        if gate == 3:
            if data.get('operation') not in ('있음', '없음'):
                raise ReviewError('홈페이지 유무를 선택하세요.')
            if data['operation'] != source['operation'] and not note:
                raise ReviewError('자동 결과를 변경한 근거를 기록하세요.')
        op = self.operation(review, source)
        if gate == 4 and op == '없음':
            if not source['search_decision']:
                raise ReviewError('검색 결과가 비어 있습니다. 검색 재처리를 요청하거나 보류하세요.')
            if source['search_decision'] not in ('제호일치_미발견', '도메인만료_폐업의심'):
                raise ReviewError('검색 결과에 후속 처리가 필요합니다. 재처리를 요청하세요.')
            if not note:
                raise ReviewError('검색 근거를 확인한 내용을 기록하세요.')
        if gate == 5 and op == '있음':
            items = data.get('fields', {})
            for name in FIELDS:
                item = items.get(name, {})
                if item.get('decision') not in ('present', 'absent'):
                    raise ReviewError(f'{name}: 게재 또는 미게재를 확인하세요.')
                if not str(item.get('evidence', '')).strip():
                    raise ReviewError(f'{name}: 확인한 근거 문구·위치를 기록하세요.')
                if item['decision'] == 'present' and not str(item.get('value', '')).strip():
                    raise ReviewError(f'{name}: 인정한 값을 입력하세요.')
                if item.get('method') not in ('direct', 'substitute', 'cross', 'article', 'absent'):
                    raise ReviewError(f'{name}: 인정 방식을 선택하세요.')
                if item.get('method') == 'article' and name != '발행연월일':
                    raise ReviewError('기사 날짜 인정은 발행연월일에서만 사용할 수 있습니다.')
                if item['decision'] == 'absent' and item.get('method') != 'absent':
                    raise ReviewError(f'{name}: 미게재 확인 시 인정 방식도 미게재로 선택하세요.')
                if item['decision'] == 'present' and item.get('method') == 'absent':
                    raise ReviewError(f'{name}: 게재 인정 방식을 선택하세요.')
            present = lambda n: items[n]['decision'] == 'present'
            if present('등록연월일') and not present('발행연월일'):
                raise ReviewError('등록연월일 인정 시 발행연월일도 인정해야 합니다.')
            if present('발행연월일') and items['발행연월일']['method'] != 'article' and not present('등록연월일'):
                raise ReviewError('기사 날짜 외 발행연월일을 인정하면 등록연월일도 인정해야 합니다.')
            if items['등록연월일']['method'] == 'article':
                raise ReviewError('기사 업로드 날짜는 등록연월일 근거로 인정할 수 없습니다.')
            if present('발행인') != present('편집인'):
                raise ReviewError('발행인·편집인 교차 인정 규칙을 확인하세요.')
        if gate == 6 and op == '있음':
            for name in ['등록번호', '발행인', '편집인', '발행소 주소', '발행소 전화번호']:
                if data.get('comparisons', {}).get(name) not in ('일치', '부분일치', '불일치', '미검출', '등록값없음'):
                    raise ReviewError(f'{name}: 등록정보 대조 결과를 선택하세요.')
        if gate == 7:
            if not note:
                raise ReviewError('최종 판정을 뒷받침하는 증빙·잔여 사유 검토 내용을 남기세요.')
            if op == '있음' and not source['assets']:
                raise ReviewError('화면 증빙이 없습니다. 재수집 후 검수하세요.')
        if gate == 8:
            if not note:
                raise ReviewError('최종 승인 의견을 남기세요.')

    def request_job(self, media_id, body):
        stage, reason = body.get('stage'), str(body.get('reason', '')).strip()
        if stage not in ('capture', 'search', 'review', 'pubdate', 'annotate') or not reason:
            raise ReviewError('재처리 종류와 사유를 입력하세요.')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.authorize(c, media_id)
            r = c.execute('SELECT * FROM businesses WHERE id=?', (media_id,)).fetchone()
            if not r:
                raise ReviewError('배정된 사업체가 아닙니다.', 404)
            self.check_version(r, body)
            old = json.loads(r['review']); review = json.loads(r['review'])
            start = {'capture': 2, 'search': 4, 'review': 5, 'pubdate': 5, 'annotate': 7}[stage]
            for n in range(start, 9):
                review.get('gates', {}).pop(str(n), None)
            c.execute('INSERT INTO jobs(media_id,stage,reason,status,source_version,created_at) VALUES(?,?,?,?,?,?)',
                      (media_id, stage, reason[:4000], 'queued', r['version'], now()))
            c.execute('UPDATE businesses SET review=?,state=?,version=version+1,updated_at=? WHERE id=?',
                      (json.dumps(review, ensure_ascii=False), 'reprocess', now(), media_id))
            self.event(c, media_id, 'job:' + stage, old, {'review': review, 'reason': reason, 'status': 'queued'})
        return self.detail(media_id)

    def export(self, completed=False):
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        writer.writerow(['출력구분', '언론사_id', '제호', '자동판정', '사람검토결과', '검토상태', '검토버전', '검토기록_JSON'])
        with self.connect() as c:
            where, params = self.scope()
            rows = c.execute('SELECT * FROM businesses' + where + ' ORDER BY ordinal', params).fetchall()
            if completed and any(r['state'] != 'completed' for r in rows):
                raise ReviewError('미완료 건이 있어 완료본을 출력할 수 없습니다. 초안을 이용하세요.', 409)
            for r in rows:
                s, v = json.loads(r['source']), json.loads(r['review'])
                verdict = ''
                if r['state'] == 'completed':
                    if self.operation(v, s) == '없음':
                        verdict = '홈페이지없음'
                    else:
                        fields = v['gates']['5']['data']['fields']
                        verdict = '준수' if all(fields[n]['decision'] == 'present' for n in FIELDS) else '미준수'
                values = ['완료본' if completed else '검토 중 초안', r['id'], s['name'], s['decision'], verdict,
                          r['state'], r['version'], json.dumps(v, ensure_ascii=False)]
                writer.writerow([("'" + x if isinstance(x, str) and x[:1] in '=+-@\t\r' else x) for x in values])
        return ('\ufeff' + out.getvalue()).encode('utf-8')
