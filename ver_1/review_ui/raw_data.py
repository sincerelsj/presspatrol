"""Read-only connection to the configured run_full; reviewed snapshots are never replaced."""
import csv
import hashlib
import io
import json
import re
from pathlib import Path
from storage import FIELDS, ReviewError, now

REQUIRED = {'언론사_id', '제호_등록', '메인_url', '홈페이지_운영현황', '준수여부_LLM'}

class RawData:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root).resolve()

    def files(self):
        return [{'name': p.name, 'bytes': p.stat().st_size} for p in sorted((self.root / 'outputs').glob('verify_*.csv')) if p.is_file()]

    def read(self, name):
        if not isinstance(name, str) or name not in [p['name'] for p in self.files()]:
            raise ReviewError('연결된 outputs 폴더의 verify CSV만 선택할 수 있습니다.')
        p = (self.root / 'outputs' / name).resolve()
        if p.parent != (self.root / 'outputs').resolve():
            raise ReviewError('허용되지 않은 원본 경로입니다.', 403)
        raw = p.read_bytes()
        try:
            reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
            if not REQUIRED.issubset(reader.fieldnames or []):
                raise ReviewError('필수 컬럼이 부족합니다: ' + ', '.join(sorted(REQUIRED - set(reader.fieldnames or []))))
            rows = list(reader)
        except UnicodeError:
            raise ReviewError('UTF-8 CSV가 필요합니다.')
        ids = [r['언론사_id'] for r in rows]
        if not rows or len(ids) != len(set(ids)) or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', i) for i in ids):
            raise ReviewError('빈 자료, 중복 ID 또는 잘못된 ID가 있습니다.')
        return rows, hashlib.sha256(raw).hexdigest(), reader.fieldnames

    def preview(self, name):
        rows, digest, columns = self.read(name)
        with self.store.connect() as c:
            existing = {r[0] for r in c.execute('SELECT id FROM businesses')}
        return {'file': name, 'sha256': digest, 'count': len(rows), 'columns': columns,
                'new_count': sum(r['언론사_id'] not in existing for r in rows),
                'existing_count': sum(r['언론사_id'] in existing for r in rows),
                'sample': [{k: r.get(k, '') for k in REQUIRED} for r in rows[:5]]}

    def convert(self, row, source_file, digest):
        rid = row['언론사_id']
        fields = {f: {'value': row.get(f+'_게재값',''), 'registered': row.get(f+'_등록값',''),
                      'comparison': row.get(f+'_대조',''), 'evidence': row.get(f+'_근거',''),
                      'location': row.get(f+'_위치',''), 'method': row.get(f+'_추출방법',''),
                      'confidence': row.get(f+'_확신도','')} for f in FIELDS}
        mapping = {'name':'제호_등록','url':'메인_url','final_url':'최종URL','operation':'홈페이지_운영현황',
                   'logic':'운영현황_로직','operation_reason':'운영현황_사유','decision':'준수여부_LLM',
                   'reason':'확인필요_사유','missing_evidence':'증빙_누락','connection':'접속결과',
                   'search_decision':'검색_판정','candidate_url':'검색_후보URL','difference':'규칙_LLM_차이',
                   'rationale':'판단근거','actual_date':'발행연월일_실제','date_route':'발행연월일_확인경로'}
        result = {k:row.get(v,'') for k,v in mapping.items()}
        result.update(id=rid, fields=fields, flagged=row.get('확인필요')=='Y',
                      assets=[s for s in ('top.png','footer.png','article.png','top_annot.png','footer_annot.png') if (self.root/'shots'/f'{rid}_{s}').is_file()],
                      raw=row, source_file=source_file, source_sha256=digest,
                      raw_sha256=hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
        return result

    def import_file(self, user, name, expected_hash):
        rows, digest, _ = self.read(name)
        if digest != expected_hash:
            raise ReviewError('미리보기 이후 원본 파일이 바뀌었습니다. 다시 확인하세요.', 409)
        inserted = kept = conflicts = 0
        # Read and convert before transaction; filesystem I/O must not hold SQLite's write lock.
        converted = [(r['언론사_id'], self.convert(r,name,digest)) for r in rows]
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            previous = c.execute('SELECT source_file FROM imports ORDER BY id LIMIT 1').fetchone()
            if previous and previous['source_file'] != name:
                raise ReviewError('다른 기준일 파일은 새 data-dir에서 별도 회차로 가져오세요.', 409)
            offset = c.execute('SELECT coalesce(max(ordinal),0) FROM businesses').fetchone()[0] + 1
            for rid, value in converted:
                old = c.execute('SELECT source,version FROM businesses WHERE id=?', (rid,)).fetchone()
                if old:
                    prior = json.loads(old['source'])
                    if prior.get('raw_sha256') == value['raw_sha256']:
                        kept += 1
                    elif not prior.get('raw_sha256') and old['version'] == 0:
                        # Untouched demo records gain full raw provenance; preserve embedded evidence.
                        for k in ('audit','search_result','evidence_map'):
                            if k in prior: value[k] = prior[k]
                        c.execute('UPDATE businesses SET source=? WHERE id=?', (json.dumps(value,ensure_ascii=False),rid))
                        kept += 1
                    else:
                        conflicts += 1  # Never overwrite a review with a changed source.
                    continue
                c.execute('INSERT INTO businesses(id,ordinal,source) VALUES(?,?,?)', (rid,offset+inserted,json.dumps(value,ensure_ascii=False)))
                inserted += 1
            c.execute('INSERT INTO imports(source_file,sha256,row_count,inserted,kept,conflicts,actor,created_at) VALUES(?,?,?,?,?,?,?,?)',
                      (name,digest,len(rows),inserted,kept,conflicts,user['username'],now()))
            meta = {'source_name':name,'source_sha256':digest,'source_root':str(self.root),'demo':False,
                    'criteria':'260813 개발 기준','storage_label':'data/reviews.sqlite3',
                    'base_date':name.removeprefix('verify_').removesuffix('.csv')}
            for k,v in meta.items():c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False)))
            result = {'inserted':inserted,'kept':kept,'conflicts':conflicts,'count':len(rows),'sha256':digest}
            self.store.for_user(user).event(c,'*','raw:import',{},result)
        return result

    def enrich(self, detail):
        rid = detail['id']
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',rid):return detail
        for key,folder,suffix in [('audit','audit','.json'),('search_result','work','.resolved.json'),('evidence_map','evidence','.json')]:
            if key in detail['source']:continue
            p=(self.root/folder/(rid+suffix)).resolve()
            if p.parent == (self.root/folder).resolve() and p.is_file():
                try:detail['source'][key]=json.loads(p.read_text(encoding='utf-8-sig'))
                except (ValueError,UnicodeError):detail['source'][key]={'error':'원본 파일 읽기 실패'}
        return detail
