"""One-time local administrator bootstrap; credentials never go to console/logs."""
import argparse
import os
import secrets
from pathlib import Path
from accounts import Accounts
from storage import Store

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent/'data')
    p.add_argument('--username',default='admin')
    p.add_argument('--reviewer',help='선택: 초기 검토자 계정과 기존 자료 최대 1,000건 배정')
    args=p.parse_args()
    store=Store(args.data_dir);accounts=Accounts(store)
    with store.connect() as c:
        if c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            p.error('관리자가 이미 존재합니다. 초기 비밀번호를 다시 만들지 않습니다.')
    password=secrets.token_urlsafe(20)
    credential=store.root/'admin_initial_credentials.txt'
    # Exclusive create prevents replacing an existing secret; chmod precedes the write.
    fd=os.open(credential,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        admin=accounts.create(args.username,'관리자',password,'admin')
        reviewer_text=''
        if args.reviewer:
            reviewer_password=secrets.token_urlsafe(20)
            reviewer=accounts.create(args.reviewer,'검토자 01',reviewer_password,'reviewer',admin)
            with store.connect() as c:
                count=c.execute('SELECT count(*) FROM businesses WHERE id NOT IN (SELECT media_id FROM assignments)').fetchone()[0]
            if count:accounts.assign(admin,{'user_id':reviewer['id'],'count':min(count,1000)})
            reviewer_text='\n검토자 초기 계정\n아이디: '+args.reviewer+'\n비밀번호: '+reviewer_password+'\n'
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write('관리자 초기 계정\n아이디: '+args.username+'\n비밀번호: '+password+'\n'+reviewer_text+'\n로그인 후 계정 메뉴에서 비밀번호를 변경하세요. 이 파일을 공유 저장소에 올리지 마세요.\n')
    except Exception:
        try:os.close(fd)
        except OSError:pass
        raise
    print('관리자 생성 완료. 초기 자격 증명 파일: '+str(credential))
