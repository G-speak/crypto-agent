#!/usr/bin/env python3
"""Real Gate GET-only smoke + an isolated paper roundtrip; never sends orders."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from clients.gate_readonly import GateReadOnly
from clients.order_journal import OrderJournal, number, normalize
from clients.paper_account import PaperAccount


def read_credentials(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'r',encoding='utf-8') as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>65536):
            raise PermissionError('untrusted_credential_file')
        values={}
        for line in stream:
            text=line.strip()
            if text and not text.startswith('#') and '=' in text:
                key,value=text.split('=',1)
                if key.strip() in ('GATEIO_API_KEY','GATEIO_API_SECRET'):
                    values[key.strip()]=value.strip().strip('\"').strip("'")
        if not all(values.get(k) for k in ('GATEIO_API_KEY','GATEIO_API_SECRET')):
            raise ValueError('missing_credentials')
        return values


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env-file',help='Optional existing credential file; stays on this server')
    args=parser.parse_args()
    report={'notifications_enabled':False,'live_orders_enabled':False}
    readers=[]
    failed=False
    with tempfile.TemporaryDirectory(prefix='crypto_readonly_smoke_') as temp:
        reader=GateReadOnly();readers.append(reader)
        try:
            row=reader.ticker('BTC/USDT');price=number(row.get('last'),True)
            report['public_market']='ok'
            now=time.time();quotes={'BTC/USDT':{'price':str(price),'observed_at':now}}
            paper=PaperAccount(Path(temp)/'paper'/'ledger.db',1000)
            buy=paper.place('BTC/USDT','BUY',20,quotes,now=now,key='smoke-buy')
            sell=paper.place('BTC/USDT','SELL',100,quotes,now=now,key='smoke-sell')
            report['paper_roundtrip']=bool(buy['success'] and sell['success'] and not paper.snapshot()['positions'])
            report['paper_net_after_assumed_costs']=paper.snapshot()['realized_net']
            if not report['paper_roundtrip']:failed=True
        except Exception as exc:
            report['public_market']='failed';report['public_error_type']=type(exc).__name__;failed=True
        if args.env_file:
            try:
                credentials=read_credentials(args.env_file)
                private=GateReadOnly(credentials['GATEIO_API_KEY'],credentials['GATEIO_API_SECRET']);readers.append(private)
                accounts=private.accounts()
                for account in accounts:
                    number(account.get('available'));number(account.get('locked'))
                report['private_account_read']='ok'
                checked=False
                for symbol in ('BTC/USDT','ETH/USDT','SOL/USDT','BNB/USDT'):
                    orders=private.orders(symbol)
                    candidate=next((r for r in orders if r.get('account')=='spot' and str(r.get('id','')).isdigit()
                                    and r.get('side') in ('buy','sell')),None)
                    if not candidate:continue
                    journal=OrderJournal(Path(temp)/'orders'/'journal.db')
                    journal.prepare('smoke-existing','smoke-account',symbol,candidate['side'],{'import_existing':True})
                    journal.attach('smoke-existing',str(candidate['id']))
                    result=journal.reconcile('smoke-existing',private)
                    report['existing_order_reconciliation']=result['state']
                    report['existing_order_fills_confirmed']=bool(result.get('evidence') and result['evidence']['exchange_confirmed'])
                    if result['state']=='unknown':
                        # Fixed validation labels only; no response bodies or account values.
                        try:normalize(private.order(str(candidate['id']),symbol),private.trades(str(candidate['id']),symbol),
                                      str(candidate['id']),symbol,candidate['side'])
                        except ValueError as exc:report['evidence_validation']=str(exc)
                        except Exception as exc:report['evidence_error_type']=type(exc).__name__
                        failed=True
                    checked=True;break
                if not checked:report['existing_order_reconciliation']='no_recent_spot_order_available'
            except Exception as exc:
                report['private_account_read']='failed';report['private_error_type']=type(exc).__name__;failed=True
        else:
            report['private_account_read']='not_requested'
    attempts=[request for reader in readers for request in reader.requests_made]
    report['http_request_attempts']=len(attempts)
    report['mutation_http_requests']=sum(r['method']!='GET' for r in attempts)
    report['ok']=not failed
    print(json.dumps(report,ensure_ascii=False))
    return 0 if not failed else 1

if __name__=='__main__':raise SystemExit(main())
