"""
企业微信配置 - 多租户加载器
从 clients/ 目录下的 JSON 文件加载配置
通过环境变量 CRYPTO_CLIENT 指定客户端，默认 mom
"""
import os
import json

# 哪个客户端？默认 mom
CLIENT = os.environ.get("CRYPTO_CLIENT", "mom")

# 加载配置文件
CFG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients")
CFG_PATH = os.path.join(CFG_DIR, f"{CLIENT}.json")

if not os.path.exists(CFG_PATH):
    raise FileNotFoundError(f"客户端配置文件未找到: {CFG_PATH}")

with open(CFG_PATH, "r", encoding="utf-8") as _f:
    _cfg = json.load(_f)

# ==================== 导出配置 ====================

# 企业微信
CORPID = _cfg["wechat"]["corp_id"]
AGENTID = _cfg["wechat"]["agent_id"]
SECRET = _cfg["wechat"]["secret"]
TOKEN = _cfg["wechat"]["token"]
ENCODING_AES_KEY = _cfg["wechat"]["aes_key"]

# API Key
AI_API_KEY = _cfg["api"]["ai_api_key"]
DEEPSEEK_API_KEY = _cfg["api"].get("deepseek_api_key", "")
YUNWU_API_KEY = _cfg["api"].get("yunwu_api_key", AI_API_KEY)  # 备用：Yunwu.ai 付费通道

# 监控币种
WATCHLIST = [(k, v) for k, v in _cfg["watchlist"].items()]

# 其他
TOUSER = _cfg.get("touser", "@all")
PORT = _cfg.get("port", 8080)
MAJOR_COINS = _cfg.get("major_coins", ["BTC", "ETH"])
ALERT_COOLDOWN = _cfg.get("alert_cooldown", 7200)
MONITOR_INTERVAL = _cfg.get("monitor_interval", 300)
PUSH_TIMES = _cfg.get("push_times", ["08:00"])

# 客户端名称（用于日志/文件路径）
CLIENT_NAME = _cfg.get("name", CLIENT)

# Explicit callback allowlist; empty/malformed configuration denies all messages.
_callback_users = _cfg.get("callback_allowed_users", [])
CALLBACK_ALLOWED_USERS = (frozenset(_callback_users)
                          if type(_callback_users) is list
                          and all(type(user) is str and user and user != "@all" for user in _callback_users)
                          else frozenset())

NOTIFICATIONS_ENABLED = _cfg.get("notifications_enabled") is True
