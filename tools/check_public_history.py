"""Verify all reachable public commits and blob versions without printing matched content."""
import re
from check_secrets import git, scan_blob

def main():
    failures=[]
    seen=set()
    commits=git('rev-list','--all').decode().splitlines()
    for commit in commits:
        emails=git('show','-s','--format=%ae%n%ce',commit).splitlines()
        for email in emails:
            allowed=re.fullmatch(rb'(?:[0-9]+\+)?[A-Za-z0-9_-]+@users\.noreply\.github\.com',email)
            if not allowed and email!=b'noreply'+b'@'+b'github.com':
                failures.append((commit[:7],'non-private-author-or-committer-email'))
        entries=git('ls-tree','-r','-z',commit).split(b'\0')
        for entry in filter(None,entries):
            meta,name=entry.split(b'\t',1)
            mode,kind,oid=meta.split()
            if kind!=b'blob':continue
            key=(name,oid)
            if key in seen:continue
            seen.add(key)
            filename=name.decode('utf-8')
            for rule in scan_blob(filename,git('cat-file','blob',oid.decode())):
                failures.append((filename,rule))
    for target,rule in sorted(set(failures)):
        print(f'BLOCKED {target}: {rule}')
    print(f'Public-history checks: {len(commits)} commit(s), {len(seen)} file versions, {len(failures)} issue(s).')
    return int(bool(failures))

if __name__=='__main__':raise SystemExit(main())
