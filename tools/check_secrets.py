"""Check Git blobs without reading .env, logging secrets, or calling any API."""
import argparse
import io
from pathlib import PurePosixPath
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

PATTERNS = {
    'provider-key': re.compile(rb'(?:sk-ant-[A-Za-z0-9_-]{24,}|sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,})'),
    'github-token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'aws-access-key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'private-key': re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
}
SAMPLE = 'examples/registry/registry_sample.xlsx'
XML_NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
EMAIL_RE = re.compile(rb'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
PHONE_RE = re.compile(rb'(?<![A-Za-z0-9])(?:0\d{1,2}[- .)]?\d{3,4}[- .]?\d{4}|1[568]\d{2}[- .]\d{4})(?![A-Za-z0-9])')
# Explicitly synthetic fixture values only; real-case regression contacts were removed.
FIXTURE_PHONES = {b''.join(parts) for parts in [
    (b'033',b'123',b'4567'), (b'033',b'123',b'4568'), (b'02',b'3456',b'7890'),
    (b'033',b'999',b'1234'), (b'051',b'777',b'8888'), (b'043',b'222',b'3333'),
    (b'033',b'640',b'1234'), (b'02',b'111',b'2222'), (b'064',b'555',b'6666'),
    (b'031',b'111',b'2222'), (b'033',b'111',b'5555'),
]}
FIXTURE_PATH = 'news-audit-skill/tests/disclosure_fixture_e2e.py'

def contact_issues(name, payload):
    issues=[]
    for match in EMAIL_RE.finditer(payload):
        domain=match.group().split(b'@',1)[1].lower()
        if not (domain in {b'example.com',b'example.org',b'example.net'} or domain.endswith((b'.invalid',b'.example'))):
            issues.append('non-example-email')
    for match in PHONE_RE.finditer(payload):
        digits=re.sub(rb'\D',b'',match.group())
        if set(digits)=={48}:continue
        if name==FIXTURE_PATH and digits in FIXTURE_PHONES:continue
        issues.append('unapproved-phone-number')
    if re.search(rb'(?:[A-Za-z]:[/\\]Users[/\\][A-Za-z0-9_\x80-\xff][^\s/\\]*|/(?:Users|home)/[A-Za-z0-9_\x80-\xff][^\s/]*)',payload):
        issues.append('personal-home-path')
    return issues

def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL)

def scan_blob(name, data):
    """Return rule names only. Never return matched bytes."""
    problems = []
    p = PurePosixPath(name)
    if (p.name.startswith('.env') and p.name != '.env.example') or p.suffix.lower() in {'.pem','.key','.p12','.pfx','.sqlite3','.db'}:
        problems.append('forbidden-config-or-database')
    if any(part in {'.claude','.codex','.agents','data','data_test','shots','audit','evidence','work','outputs','overrides'} or part.startswith('run_') for part in p.parts[:-1]):
        problems.append('runtime-data')
    if p.suffix.lower() in {'.xlsx','.xls','.xlsm','.csv','.tsv','.pdf','.png','.jpg','.jpeg'} and name != SAMPLE:
        problems.append('unapproved-data-or-evidence')
    payloads = [data]
    if name == SAMPLE:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                sheets = [n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n)]
                shared=[]
                if 'xl/sharedStrings.xml' in names:
                    shared=[''.join(si.itertext()) for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si',XML_NS)]
                def cell_value(cell):
                    v=cell.find('s:v',XML_NS)
                    if cell.get('t')=='s':return shared[int(v.text)] if v is not None else ''
                    if cell.get('t')=='inlineStr':return ''.join(cell.find('s:is',XML_NS).itertext())
                    return v.text if v is not None else ''
                if len(sheets) != 1:
                    problems.append('sample-sheet-count')
                for entry in sheets:
                    xml = ET.fromstring(z.read(entry))
                    rows = xml.findall('s:sheetData/s:row',XML_NS)
                    if len(rows) != 21:
                        problems.append('sample-must-have-20-records')
                    if rows:
                        headers={re.sub(r'\d','',c.get('r','')):cell_value(c) for c in rows[0].findall('s:c',XML_NS)}
                        for row in rows[1:]:
                            for cell in row.findall('s:c',XML_NS):
                                value=cell_value(cell)
                                header=headers.get(re.sub(r'\d','',cell.get('r','')),'')
                                if header in {'발행소 주소','발행소 전화번호','발행소 우편번호','인쇄소주소','인쇄소전화번호'} and value:
                                    problems.append('sample-contact-field-must-be-blank')
                                if header in {'발행인','편집인'} and value and not re.fullmatch(r'가상담당자\d{3}',value):
                                    problems.append('sample-person-must-be-synthetic')
                                if header in {'URL','발행소 홈페이지'} and value and not value.endswith('.example.invalid'):
                                    problems.append('sample-url-must-be-invalid-domain')
                    if xml.findall('.//s:f',XML_NS):
                        problems.append('sample-formulas')
                if any(n.startswith(('xl/externalLinks/','xl/embeddings/')) or n.endswith('vbaProject.bin') for n in names):
                    problems.append('sample-external-or-embedded-content')
                payloads.extend(z.read(n) for n in names if n.endswith('.xml'))
        except (zipfile.BadZipFile, ET.ParseError, KeyError):
            problems.append('invalid-sample-workbook')
    for label, pattern in PATTERNS.items():
        if any(pattern.search(payload) for payload in payloads):
            problems.append(label)
    contact_payloads=payloads
    if p.suffix.lower()=='.svg':
        # Geometry is numeric; contacts can only be text/metadata in these outlined diagrams.
        try:contact_payloads=[' '.join(ET.fromstring(data).itertext()).encode('utf-8')]
        except ET.ParseError:problems.append('invalid-svg')
    elif name==SAMPLE:
        contact_payloads=payloads[1:]
    for payload in contact_payloads:
        problems.extend(contact_issues(name,payload))
    if p.name == '.env.example':
        for line in data.decode('utf-8-sig').splitlines():
            if line.startswith('ANTHROPIC_API_KEY=') and line.split('=',1)[1].strip() != 'your_API_key':
                problems.append('example-key-must-be-placeholder')
    return sorted(set(problems))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--staged',action='store_true')
    group.add_argument('--tracked',action='store_true')
    args=parser.parse_args()
    if args.staged:
        # Inspect the complete prospective commit, including previously tracked files.
        names=git('ls-files','-z').decode('utf-8').split('\0')
    else:
        names=git('ls-tree','-r','--name-only','-z','HEAD').decode('utf-8').split('\0')
    failures=[]
    for name in filter(None,names):
        data=git('show',(':' if args.staged else 'HEAD:')+name)
        failures.extend((name,rule) for rule in scan_blob(name,data))
    for name,rule in failures:
        print(f'BLOCKED {name}: {rule}',file=sys.stderr)
    print(f'Credential/data checks: {len([n for n in names if n])} Git blobs, {len(failures)} issue(s). No key values displayed.')
    return 1 if failures else 0

if __name__=='__main__':
    raise SystemExit(main())
