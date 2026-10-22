#!/usr/bin/env python3
"""Run the six suites in a fresh copy with fake config, clean env and no sockets."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
SUITES=('p0_tests.py','p0_fixes_tests.py','audit_e2e.py','safety_p1_tests.py',
        'callback_p1_tests.py','reconciliation_risk_tests.py')
GUARD='''import socket\ndef blocked(*a, **k):\n    raise OSError("offline tests: network disabled")\nsocket.socket.connect=blocked\nsocket.socket.connect_ex=blocked\nsocket.create_connection=blocked\nsocket.getaddrinfo=blocked\n'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--json-output');args=parser.parse_args()
    results=[]
    with tempfile.TemporaryDirectory(prefix='crypto_safe_tests_') as temp:
        temp=Path(temp);work=temp/'source';work.mkdir();home=temp/'home';home.mkdir()
        for p in ROOT.rglob('*.py'):
            if any(part in ('.git','.venv','venv','__pycache__') for part in p.relative_to(ROOT).parts):continue
            dest=work/p.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
        guard=temp/'guard';guard.mkdir();(guard/'sitecustomize.py').write_text(GUARD)
        fixture={
            'name':'mom','wechat':dict.fromkeys(('corp_id','agent_id','secret','token','aes_key'),'OFFLINE_FAKE'),
            'api':{'ai_api_key':'OFFLINE_FAKE'},'watchlist':{'BTC':'btcusdt','ETH':'ethusdt'},
            'major_coins':['BTC','ETH','SOL','BNB'],'notifications_enabled':False,'callback_allowed_users':[]}
        (work/'clients'/'mom.json').write_text(json.dumps(fixture))
        dependencies=[p for p in sys.path if p and Path(p).is_dir() and Path(p).resolve()!=ROOT]
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(home),
             'PYTHONPATH':os.pathsep.join([str(guard),*dependencies]),
             'PYTHONDONTWRITEBYTECODE':'1','CRYPTO_CLIENT':'mom'}
        for name in SUITES:
            try:
                p=subprocess.run([sys.executable,'-B',str(work/'tests'/name)],cwd=work,env=env,
                                 capture_output=True,text=True,timeout=180)
                result={'suite':name,'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
            except subprocess.TimeoutExpired:
                result={'suite':name,'returncode':124,'stdout':'','stderr':'suite timed out'}
            results.append(result)
            print(name+': '+('PASS' if result['returncode']==0 else 'FAIL'),flush=True)
            if result['returncode']:print(result['stdout']+result['stderr'])
        for path in work.rglob('*.py'):compile(path.read_bytes(),str(path),'exec')
    report={'network_disabled':True,'fake_configuration':True,'results':results,
            'ok':all(r['returncode']==0 for r in results)}
    if args.json_output:
        path=Path(args.json_output);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
