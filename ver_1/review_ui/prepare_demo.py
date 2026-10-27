"""Import a deterministic 1,000-business assignment without changing run_full."""
import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from storage import Store, FIELDS, now

def prepare(source_root, data_dir, count=1000, seed=20261020):
    root = Path(source_root).resolve()
    source_file = root / 'outputs' / 'verify_2026-09-03.csv'
    rows = list(csv.DictReader(source_file.open(encoding='utf-8-sig', newline='')))
    if count < 1 or count > len(rows):
        raise ValueError('배정 건수는 1~전체 행 수 범위여야 합니다.')
    ids = [r['언론사_id'] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('원본에 중복 ID가 있습니다.')
    random.Random(seed).shuffle(rows)
    selected = rows[:count]
    store = Store(data_dir)
    manifest = {'assignment_id': 'demo-1000-001', 'reviewer': '검토자 01', 'count': count,
                'base_date': '2026-09-03', 'criteria': '260813 개발 기준',
                'source_root': str(root), 'source_name': source_file.name,
                'source_sha256': hashlib.sha256(source_file.read_bytes()).hexdigest(),
                'seed': seed, 'created_at': now(), 'demo': True,
                'storage_label': 'data/reviews.sqlite3', 'setup': {'confirmed': False}}
    with store.connect() as c:
        if c.execute('SELECT count(*) FROM businesses').fetchone()[0]:
            raise ValueError('이미 데이터가 있습니다. 새 data-dir로 생성하세요. 기존 검토 기록은 덮어쓰지 않습니다.')
        for key, value in manifest.items():
            c.execute('INSERT INTO meta VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
        for index, row in enumerate(selected):
            rid = row['언론사_id']
            assets = [suffix for suffix in ('top.png', 'footer.png', 'article.png', 'top_annot.png', 'footer_annot.png')
                      if (root / 'shots' / f'{rid}_{suffix}').is_file()]
            fields = {f: {'value': row.get(f + '_게재값', ''), 'registered': row.get(f + '_등록값', ''),
                          'comparison': row.get(f + '_대조', ''), 'evidence': row.get(f + '_근거', ''),
                          'location': row.get(f + '_위치', ''), 'method': row.get(f + '_추출방법', ''),
                          'confidence': row.get(f + '_확신도', '')} for f in FIELDS}
            data = {'id': rid, 'name': row['제호_등록'], 'url': row['메인_url'], 'final_url': row['최종URL'],
                    'operation': row['홈페이지_운영현황'], 'logic': row['운영현황_로직'],
                    'operation_reason': row['운영현황_사유'], 'decision': row['준수여부_LLM'],
                    'flagged': row['확인필요'] == 'Y', 'reason': row['확인필요_사유'],
                    'missing_evidence': row['증빙_누락'], 'connection': row['접속결과'],
                    'search_decision': row['검색_판정'], 'candidate_url': row['검색_후보URL'],
                    'difference': row['규칙_LLM_차이'], 'rationale': row['판단근거'],
                    'actual_date': row['발행연월일_실제'], 'date_route': row['발행연월일_확인경로'],
                    'fields': fields, 'assets': assets}
            # Existing audit/resolve files are evidence, never new agent judgments.
            for key, folder, suffix in [('audit', 'audit', '.json'), ('search_result', 'work', '.resolved.json'),
                                         ('evidence_map', 'evidence', '.json')]:
                p = root / folder / (rid + suffix)
                if p.is_file():
                    try:
                        data[key] = json.loads(p.read_text(encoding='utf-8-sig'))
                    except (ValueError, UnicodeError):
                        data[key] = {'error': '원본 JSON 읽기 실패'}
            c.execute('INSERT INTO businesses(id,ordinal,source) VALUES(?,?,?)',
                      (rid, index, json.dumps(data, ensure_ascii=False)))
    # Portable allocation list makes the seed-based sample independently reviewable.
    (store.root / 'assignment.json').write_text(json.dumps({**manifest, 'ids': [r['언론사_id'] for r in selected]},
                                                       ensure_ascii=False, indent=2), encoding='utf-8')
    return {'count': count, 'decisions': dict(Counter(r['준수여부_LLM'] for r in selected)), 'database': str(store.db)}

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] / 'run_full')
    p.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent / 'data')
    p.add_argument('--count', type=int, default=1000)
    p.add_argument('--seed', type=int, default=20261020)
    args = p.parse_args()
    print(json.dumps(prepare(args.source, args.data_dir, args.count, args.seed), ensure_ascii=False, indent=2))
