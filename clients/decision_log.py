#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构化决策日志（P0-5，2026-10-22）

设计约束（用户授权范围）：
  · 只做观测，不参与任何决策，不改变任何返回值；
  · 任何写入失败（磁盘满、只读、权限）都只返回 False，绝不抛异常；
  · 内容一律脱敏后再落盘；
  · 按天分文件 + 单文件超限自动切 .1，便于轮转。

用法：from clients import decision_log as dlog
      dlog.event("shallow_decision", coin="BTC", action="HOLD", ...)
"""
import json
import os
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA_VER = 1
# 最近一次写入失败的原因（供调用方/测试诊断；不代表任何业务状态）
LAST_ERROR = ""
TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.expanduser("~/.hermes/logs/decisions")
MAX_BYTES = 20 * 1024 * 1024      # 单文件 20MB 后切 .1

# 脱敏规则（P0-B 修复）：必须把"前缀 + 凭据"整体替换，不能只替换前缀。
# 顺序敏感：
#   ① Authorization / Proxy-Authorization 头 → 整行值全抹（含后面的 Bearer/Basic 与凭据）
#   ② 独立的 Bearer <token>（JSON 值、异常消息、普通文本里都会出现，无冒号）
#   ③ 独立的 Basic <base64>（≥8 位，避免误伤英文单词 basic）
#   ④ sk- 形式 API Key   ⑤ 云厂商 AK/AS   ⑥ 超长无分隔串（企微 secret/token/aes_key）
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(proxy-authorization|authorization)\b\s*[:=]\s*[^\r\n;\"|}\]]+"),
     r"\1: ***REDACTED***"),
    (re.compile(r"(?i)\b(bearer)\s+[^\s,;\"'}\)\]]+"), r"\1 ***REDACTED***"),
    (re.compile(r"(?i)\b(basic)\s+([A-Za-z0-9+/=]{8,})"), r"\1 ***REDACTED***"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), "sk-***REDACTED***"),
    (re.compile(r"(AKIA|ASIA)[A-Z0-9]{12,}"), "AWS***REDACTED***"),
    (re.compile(r"[A-Za-z0-9_\-]{32,}"), "***REDACTED_LONGSTR***"),
)


# 敏感字段名匹配规则：键名归一化成"小写 + 只保留字母数字"后，做**子串包含**判断。
# 这样 Authorization / authorization / AUTHORIZATION / x-api-key / x_api_key / apiKey /
# api_secret / apiSecret / yunwu_api_key / deepseek_api_key / gateio_api_secret /
# refreshToken / client_secret … 全部命中，不需要为每种写法单独列一条。
# 取舍：宁可多抹（如 "secretary" 也会被抹），也不漏。
_SENSITIVE_FRAGMENTS = (
    "authorization", "apikey", "apisecret", "secret", "token",
    "password", "passwd", "cookie", "privatekey", "credential", "bearer",
)


def _norm_key(k):
    """归一化键名：小写 + 去掉所有非字母数字字符。"""
    return re.sub(r"[^a-z0-9]", "", str(k).strip().lower())


def _safe_key_name(k):
    """键名脱敏（键本身可能就是一个令牌）。

    三层判定：
      ① 命中正则（sk- / Bearer / Authorization / AKIA / ≥32 位长串）→ 按正则替换
      ② 否则：归一化后长度 ≥12 且同时含字母与数字（形如 API Key / 令牌）→ 整体替换
      ③ 其余视为"字段名"原样保留（如 authorization / refreshToken / scan_id / percentage）
    说明：纯字母且很短的裸串（如 8 位）与正常字段名无法区分，见报告"已知局限"。
    """
    s = str(k)
    r = redact(s)
    if r != s:
        return r
    n = _norm_key(s)
    if len(n) >= 12 and any(ch.isdigit() for ch in n) and any(ch.isalpha() for ch in n):
        return "***REDACTED_KEY***"
    return s


def _dedup_key(k, taken):
    """键名先脱敏，再解决脱敏后撞名的问题，避免丢字段。"""
    base = _safe_key_name(k)
    if base not in taken:
        return base
    i = 2
    while "%s#%d" % (base, i) in taken:
        i += 1
    return "%s#%d" % (base, i)


def _is_sensitive_key(k):
    n = _norm_key(k)
    if not n:
        return False
    return any(frag in n for frag in _SENSITIVE_FRAGMENTS)


def sanitize(value, _depth=0):
    """递归脱敏（P0-B 第二轮）：
    · dict：键名命中 _SENSITIVE_KEYS → 值整体替换为掩码（无论它是字符串、短值还是嵌套对象）
    · list/tuple：逐元素递归
    · str：走正则脱敏（Authorization 头、Bearer、sk-、长串…）
    返回新对象，不修改入参；深度上限防自引用炸栈。
    """
    if _depth > 12:
        return "***REDACTED_DEPTH***"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            # ★ 键名本身也要过一遍脱敏：{ "sk-xxx": ... } 这种"用令牌当键"的写法会绕过值侧规则
            sk = _dedup_key(k, out)
            if _is_sensitive_key(k):
                out[sk] = "***REDACTED***"
            else:
                out[sk] = sanitize(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [sanitize(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return redact(bytes(value).decode("utf-8", "replace"))
        except Exception:
            return "***REDACTED_BYTES***"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # 自定义对象：先 str() 再走正则，避免 str(obj) 里夹带的 token 漏网
    try:
        return redact(str(value))
    except Exception:
        return "***REDACTED_OBJECT***"


def redact(text):
    """把疑似凭证替换掉；非字符串原样转字符串。永不抛异常。"""
    try:
        if text is None:
            return ""
        s = str(text)
        for pat, rep in _SECRET_PATTERNS:
            s = pat.sub(rep, s)
        return s
    except Exception:
        return ""


def _ensure_dir():
    """目录必须为 700。无法创建/无法纠正 → 抛异常，由 event() 转成"写入失败"。
    P0-C 修正：不再吞掉 chmod 异常（旧实现会把权限不合格的日志当成写入成功）。"""
    os.makedirs(BASE_DIR, mode=0o700, exist_ok=True)
    info = os.lstat(BASE_DIR)
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError("日志目录必须为真实目录")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o700:
        os.chmod(BASE_DIR, 0o700)
        mode = stat.S_IMODE(os.stat(BASE_DIR).st_mode)
        if mode != 0o700:
            raise PermissionError("日志目录权限无法纠正为 700（当前 %o）" % mode)


def _open_log(path):
    """以 600 权限打开（含已存在的旧文件）；O_APPEND 保证多进程追加不互相覆盖。
    P0-C 修正：打开后用 fstat 实测权限，不合格就纠正；纠正不了 → 关闭 fd 并抛异常，
    调用方（event）会返回 False，绝不向权限不合格的文件写入。"""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("日志必须为单链接普通文件")
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o600:
            os.fchmod(fd, 0o600)
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != 0o600:
            raise PermissionError("日志文件权限无法纠正为 600（当前 %o）" % mode)
        return os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        try:
            os.close(fd)
        except Exception:
            pass
        raise


def _current_path():
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    return os.path.join(BASE_DIR, "decisions-%s.jsonl" % day)


def _rotate(path):
    """超过 MAX_BYTES 时切 .1；轮转文件同样必须 600。
    P0-C 修正：若 replace 之后权限纠正失败，则**回滚**（把 .1 挪回原名）并抛异常，
    避免遗留一个权限不合规的 .1 文件；本次事件由 event() 返回 False。"""
    if not (os.path.exists(path) and os.path.getsize(path) >= MAX_BYTES):
        return
    target = path + ".1"
    for candidate in (path, target):
        if os.path.lexists(candidate):
            info = os.lstat(candidate)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionError("轮转路径必须为单链接普通文件")
    os.replace(path, target)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
        if mode != 0o600:
            os.chmod(target, 0o600)
            mode = stat.S_IMODE(os.stat(target).st_mode)
        if mode != 0o600:
            raise PermissionError("轮转文件权限无法纠正为 600（当前 %o）" % mode)
    except BaseException:
        # ① 回滚：把 .1 挪回原名（数据优先，不丢）
        rolled_back = False
        try:
            os.replace(target, path)
            rolled_back = True
        except Exception:
            pass
        # ② 回滚后活动文件若存在，必须修回 600；修不回来就隔离成 .unsafe，
        #    绝不允许磁盘上留下一个 644 的"活动日志文件"。
        try:
            if os.path.exists(path) and stat.S_IMODE(os.stat(path).st_mode) != 0o600:
                os.chmod(path, 0o600)
            if os.path.exists(path) and stat.S_IMODE(os.stat(path).st_mode) != 0o600:
                raise PermissionError("活动日志权限无法恢复为 600")
        except Exception as _perm_err:
            try:
                os.replace(path, path + ".unsafe")
            except Exception as _q_err:
                # chmod 与隔离同时失败：无法保证活动路径不是 644 —— 必须显式报错，
                # 让 event() 返回 False 并把原因写进 LAST_ERROR，绝不静默成功。
                raise PermissionError(
                    "活动日志权限无法恢复为 600 且隔离失败（活动文件仍为 %s，位于原路径）: %s; 原始错误: %s"
                    % (oct(stat.S_IMODE(os.stat(path).st_mode)) if os.path.exists(path) else "unknown",
                       _q_err, _perm_err)
                )
        if not rolled_back:
            raise PermissionError("轮转权限校验失败且回滚失败（数据保留在 %s）" % target)
        raise


def event(kind, **fields):
    """追加一条 JSONL 事件。成功返回 True，任何失败返回 False（绝不抛异常）。

    LAST_ERROR 语义（2026-10-22 修正）：
      · 每次调用**先清空**，因此 "" 表示"最近一次 event() 成功"，
        非空字符串表示"最近一次 event() 失败的异常类别"；
      · 它是"最近一次调用"的结果，不是累积错误列表，也不代表业务状态；
      · 仅供诊断/告警使用，不参与任何决策。
    """
    global LAST_ERROR
    LAST_ERROR = ""
    try:
        _ensure_dir()
        rec = {
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            # ★ 事件名也过一遍脱敏：event("Bearer xxx") 这类调用不能把令牌原样写进日志
            "event": _safe_key_name(kind),
            "schema_ver": SCHEMA_VER,
        }
        for k, v in fields.items():
            # 键名本身也可能夹带凭据（如 {"sk-xxx": 1}）→ 先脱敏再去重
            sk = _dedup_key(k, rec)
            if _is_sensitive_key(k):
                rec[sk] = "***REDACTED***"
            else:
                rec[sk] = sanitize(v)
        line = json.dumps(rec, ensure_ascii=False, allow_nan=False)
        path = _current_path()
        _rotate(path)
        with _open_log(path) as f:
            f.write(line + "\n")
        return True
    except Exception as e:
        # 仅输出固定诊断标签，绝不回显异常正文或路径。
        label = "日志写入失败"
        try:
            message = str(e)
            for marker in ("隔离失败", "回滚失败", "权限无法纠正", "单链接普通文件"):
                if marker in message:
                    label = marker
                    break
        except Exception:
            pass
        LAST_ERROR = "%s: %s" % (type(e).__name__, label)
        return False


def new_id(prefix):
    """生成短事件 id（如 s-1a2b3c4d5e6f）。失败时返回固定值，不抛异常。"""
    try:
        return "%s-%s" % (prefix, uuid.uuid4().hex[:12])
    except Exception:
        return "%s-0" % prefix
