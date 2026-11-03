"""Authentication and durable replay protection for self-built enterprise apps."""
import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import xml.etree.ElementTree as ET


WINDOW = 300


class CallbackRejected(ValueError):
    pass


def parse_xml(text):
    if type(text) is not str or len(text.encode("utf-8")) > 65536:
        raise CallbackRejected("invalid_size")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise CallbackRejected("invalid_xml")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise CallbackRejected("invalid_xml") from None
    if root.tag != "xml":
        raise CallbackRejected("invalid_xml")
    return root


def field(root, name):
    nodes = root.findall(name)
    if len(nodes) != 1 or list(nodes[0]) or not nodes[0].text:
        raise CallbackRejected("invalid_field")
    return nodes[0].text


def check_signature(token, args, encrypted, now=None):
    now = time.time() if now is None else now
    values = []
    for key in ("timestamp", "nonce", "msg_signature"):
        value = args.get(key, "")
        if type(value) is not str or not value or len(value) > 256:
            raise CallbackRejected("invalid_signature")
        if hasattr(args, "getlist") and len(args.getlist(key)) != 1:
            raise CallbackRejected("invalid_signature")
        values.append(value)
    timestamp, nonce, signature = values
    if not re.fullmatch(r"[0-9]{1,12}", timestamp) or abs(now - int(timestamp)) > WINDOW:
        raise CallbackRejected("expired")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", signature) or not token:
        raise CallbackRejected("invalid_signature")
    expected = hashlib.sha1("".join(sorted((token, timestamp, nonce, encrypted))).encode()).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise CallbackRejected("invalid_signature")


def decrypt(key_text, encrypted, corp_id):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    try:
        key = base64.b64decode(key_text + "=", validate=True)
        data = base64.b64decode(encrypted, validate=True)
        if len(key) != 32 or not data or len(data) % 16:
            raise ValueError()
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        clear = cipher.update(data) + cipher.finalize()
        padding = clear[-1]
        if not 1 <= padding <= 32 or clear[-padding:] != bytes([padding]) * padding:
            raise ValueError()
        clear = clear[:-padding]
        if len(clear) < 21:
            raise ValueError()
        size = int.from_bytes(clear[16:20], "big")
        if size <= 0 or 20 + size >= len(clear):
            raise ValueError()
        receive_id = clear[20 + size:].decode("utf-8")
        if not corp_id or not hmac.compare_digest(receive_id.encode(), corp_id.encode()):
            raise ValueError()
        return clear[20:20 + size].decode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise CallbackRejected("invalid_ciphertext") from None


def authenticate_text(body, args, token, key, corp_id, agent_id, allowed_users, now=None):
    encrypted = field(parse_xml(body), "Encrypt")
    check_signature(token, args, encrypted, now)
    root = parse_xml(decrypt(key, encrypted, corp_id))
    if field(root, "ToUserName") != corp_id or field(root, "AgentID") != str(agent_id):
        raise CallbackRejected("wrong_recipient")
    if field(root, "MsgType") != "text":
        raise CallbackRejected("unsupported_message")
    user = field(root, "FromUserName")
    if user not in allowed_users:
        raise CallbackRejected("forbidden_user")
    created = field(root, "CreateTime")
    now = time.time() if now is None else now
    if not re.fullmatch(r"[0-9]{1,12}", created) or abs(now - int(created)) > WINDOW:
        raise CallbackRejected("expired_message")
    message_id = field(root, "MsgId")
    if not re.fullmatch(r"[0-9]{1,32}", message_id):
        raise CallbackRejected("invalid_message_id")
    content = field(root, "Content")
    if len(content.encode("utf-8")) > 4096:
        raise CallbackRejected("invalid_content")
    digest = hashlib.sha256(json_identity(corp_id, str(agent_id), user, message_id)).hexdigest()
    return user, content, digest


def json_identity(*parts):
    import json
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()


def claim_message(path, digest, now=None):
    """Commit before dispatch: at-most-once admission across workers/restarts."""
    now = time.time() if now is None else now
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        raise PermissionError("untrusted replay directory")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("untrusted replay file")
    finally:
        os.close(fd)
    with context_connection(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS seen (digest TEXT PRIMARY KEY, expires REAL NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM seen WHERE expires < ?", (now,))
        cursor = db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (digest, now + WINDOW * 2 + 60))
        return cursor.rowcount == 1


def context_connection(path):
    # sqlite3.Connection.__exit__ commits/rolls back but does not close itself.
    from contextlib import contextmanager

    @contextmanager
    def connect():
        db = sqlite3.connect(str(path), timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()
    return connect()
