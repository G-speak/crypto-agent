import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
HOME_TMP=tempfile.TemporaryDirectory(prefix='callback_p1_')
os.environ['HOME']=HOME_TMP.name
from clients import callback_guard as guard
import wechat_server as server

KEY=bytes(range(32)); KEY_TEXT=base64.b64encode(KEY).decode().rstrip('=')
CORP='fake-corp'; TOKEN='FAKE_CALLBACK_TOKEN'; AGENT='1000001'


def encrypt_raw(raw):
    cipher=Cipher(algorithms.AES(KEY),modes.CBC(KEY[:16])).encryptor()
    return base64.b64encode(cipher.update(raw)+cipher.finalize()).decode()


def encrypt(message, corp=CORP):
    data=message.encode(); raw=b'0123456789abcdef'+len(data).to_bytes(4,'big')+data+corp.encode()
    pad=32-len(raw)%32
    return encrypt_raw(raw+bytes([pad])*pad)


def signed(encrypted, now=None):
    timestamp=str(int(time.time() if now is None else now)); nonce='test-nonce'
    signature=hashlib.sha1(''.join(sorted((TOKEN,timestamp,nonce,encrypted))).encode()).hexdigest()
    return dict(timestamp=timestamp,nonce=nonce,msg_signature=signature)


def message(**changes):
    values=dict(ToUserName=CORP,AgentID=AGENT,FromUserName='allowed-user',MsgType='text',
                CreateTime=str(int(time.time())),MsgId='123456789012345',Content='行情')
    values.update(changes)
    return '<xml>'+''.join('<'+k+'>'+v+'</'+k+'>' for k,v in values.items())+'</xml>'


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.patches=[]
        for name,value in [('TOKEN',TOKEN),('ENCODING_AES_KEY',KEY_TEXT),('CORPID',CORP),
                           ('AGENTID',AGENT),('CALLBACK_ALLOWED_USERS',frozenset({'allowed-user'})),
                           ('REPLAY_FILE',self.tmp.name+'/replay/seen.sqlite3')]:
            p=patch.object(server,name,value);p.start();self.addCleanup(p.stop)
        self.dispatch=patch.object(server,'_dispatch_reply').start(); self.addCleanup(patch.stopall)
        self.client=server.app.test_client()

    def post(self, inner=None, encrypted=None, args=None):
        encrypted=encrypted or encrypt(message() if inner is None else inner)
        return self.client.post('/',query_string=args or signed(encrypted),
                                data='<xml><Encrypt>'+encrypted+'</Encrypt></xml>', content_type='application/xml')

    def test_valid_once_duplicate_ack_and_restart(self):
        encrypted=encrypt(message());args=signed(encrypted)
        self.assertEqual(self.post(encrypted=encrypted,args=args).status_code,200)
        self.dispatch.assert_called_once_with('allowed-user','行情')
        self.assertEqual(self.post(encrypted=encrypted,args=args).status_code,200)
        self.dispatch.assert_called_once()
        user,content,digest=guard.authenticate_text('<xml><Encrypt>'+encrypted+'</Encrypt></xml>',args,TOKEN,KEY_TEXT,CORP,AGENT,{'allowed-user'})
        script='import sys; sys.path.insert(0,sys.argv[1]); from clients.callback_guard import claim_message; print(claim_message(sys.argv[2],sys.argv[3]))'
        p=subprocess.run([sys.executable,'-B','-c',script,str(ROOT),server.REPLAY_FILE,digest],capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr); self.assertEqual(p.stdout.strip(),'False')
        path=Path(server.REPLAY_FILE)
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(path.parent.stat().st_mode&0o777,0o700)

    def test_wrong_signature_rejected_before_decrypt(self):
        encrypted=encrypt(message());args=signed(encrypted);args['msg_signature']='0'*40
        with patch.object(guard,'decrypt') as decrypt,patch.object(guard,'claim_message') as claim:
            self.assertEqual(self.post(encrypted=encrypted,args=args).status_code,403)
            decrypt.assert_not_called();claim.assert_not_called();self.dispatch.assert_not_called()

    def test_wrong_corp_user_agent_type_time_never_dispatch(self):
        cases=[dict(ToUserName='other'),dict(AgentID='9'),dict(FromUserName='attacker'),
               dict(MsgType='event'),dict(CreateTime=str(int(time.time())-1000)),
               dict(MsgId=''),dict(MsgId='nonnumeric')]
        for changes in cases:
            with self.subTest(changes=changes), patch.object(guard,'claim_message') as claim:
                self.assertEqual(self.post(inner=message(**changes)).status_code,403)
                claim.assert_not_called();self.dispatch.assert_not_called()
        self.assertEqual(self.post(encrypted=encrypt(message(),corp='wrong-receive-id')).status_code,403)
        self.dispatch.assert_not_called()

    def test_empty_allowlist_denies_all(self):
        with patch.object(server,'CALLBACK_ALLOWED_USERS',frozenset()):
            self.assertEqual(self.post().status_code,403)
            self.dispatch.assert_not_called()

    def test_expired_and_duplicate_query_parameters(self):
        encrypted=encrypt(message())
        for stamp in (time.time()-1000,time.time()+1000):
            self.assertEqual(self.post(encrypted=encrypted,args=signed(encrypted,stamp)).status_code,403)
        args=list(signed(encrypted).items())+[('timestamp',str(int(time.time())))]
        self.assertEqual(self.post(encrypted=encrypted,args=args).status_code,403)
        self.dispatch.assert_not_called()

    def test_tampered_malformed_and_duplicate_fields(self):
        for ciphertext in ('!',base64.b64encode(b'123').decode(),encrypt_raw(b'0'*31+b'\x00'),
                           encrypt_raw(b'0'*16+b'\xff\xff\xff\xff'+b'0'*11+b'\x01')):
            with self.subTest(ciphertext=ciphertext):
                self.assertEqual(self.post(encrypted=ciphertext).status_code,403)
        self.assertEqual(self.post(inner=message().replace('</xml>','<Content>evil</Content></xml>')).status_code,403)
        self.assertEqual(self.post(inner='<!DOCTYPE xml [<!ENTITY e "payload">]><xml>&e;</xml>').status_code,403)
        self.assertEqual(self.client.post('/',data='<xml><Encrypt>').status_code,403)
        self.assertEqual(self.client.post('/',data='a'*70000).status_code,413)
        self.dispatch.assert_not_called()

    def test_replay_database_failure_never_dispatch(self):
        with patch.object(guard,'claim_message',side_effect=sqlite3.OperationalError('fixture')):
            self.assertEqual(self.post().status_code,503)
            self.dispatch.assert_not_called()

    def test_get_verification_must_have_valid_signature_and_corp(self):
        encrypted=encrypt('test echo'); args=dict(signed(encrypted),echostr=encrypted)
        response=self.client.get('/',query_string=args)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_data(as_text=True),'test echo')
        args['msg_signature']='0'*40
        with patch.object(guard,'decrypt') as decrypt:
            self.assertEqual(self.client.get('/',query_string=args).status_code,403)
            decrypt.assert_not_called()
        encrypted=encrypt('test echo',corp='wrong-corp');args=dict(signed(encrypted),echostr=encrypted)
        response=self.client.get('/',query_string=args)
        self.assertEqual(response.status_code,403)
        self.assertNotIn(encrypted,response.get_data(as_text=True))
        self.dispatch.assert_not_called()

    def test_sensitive_content_not_logged(self):
        with patch.object(server,'log') as log:
            self.assertEqual(self.post(inner=message(Content='Bearer FAKE_SECRET')).status_code,200)
            self.assertEqual(self.post(inner=message(FromUserName='attacker',Content='Bearer FAKE_SECRET')).status_code,403)
            log.assert_not_called()


class ReplayTests(unittest.TestCase):
    def test_concurrent_workers_allow_exactly_one(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'guard'/'seen.db'
            with ThreadPoolExecutor(max_workers=10) as pool:
                results=list(pool.map(lambda _: guard.claim_message(path,'same-digest'),range(20)))
            self.assertEqual(results.count(True),1)
            self.assertEqual(results.count(False),19)

    def test_symlink_and_hardlink_do_not_touch_target(self):
        for hardlink in (False,True):
            with self.subTest(hardlink=hardlink),tempfile.TemporaryDirectory() as temp:
                parent=Path(temp)/'guard';parent.mkdir(mode=0o700)
                target=Path(temp)/'victim';target.write_text('KEEP');target.chmod(0o600)
                path=parent/'seen.db'
                (os.link if hardlink else os.symlink)(target,path)
                with self.assertRaises((OSError,PermissionError)):
                    guard.claim_message(path,'digest')
                self.assertEqual(target.read_text(),'KEEP')

    def test_unsafe_directory_denies_before_creating_db(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'guard';directory.mkdir();directory.chmod(0o755)
            with self.assertRaises(PermissionError):guard.claim_message(directory/'seen.db','digest')
            self.assertEqual(list(directory.iterdir()),[])

if __name__=='__main__':unittest.main(verbosity=2)
