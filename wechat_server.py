#!/usr/bin/env python3
"""
企业微信回调服务 - 支持消息接收和回复
用户发消息 → 解析 → 执行操作 → 回复
"""

import os
import sys
import hashlib
import base64
import json
import time
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, make_response

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

app = Flask(__name__)

# ==================== 配置 ====================
from wechat_config import TOKEN, ENCODING_AES_KEY, CORPID, AGENTID, SECRET as CORPSECRET

LOG_FILE = os.path.expanduser("~/.hermes/logs/wechat_callback.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# 消息指令映射
COMMANDS = {
    "比特币": "btc", "btc": "btc", "大饼": "btc",
    "以太坊": "eth", "eth": "eth", "以太": "eth",
    "sol": "sol", "solana": "sol",
    "bnb": "bnb", "币安币": "bnb",
    "行情": "overview",
}

# 导入分析模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from crypto_monitor import analyze_symbol, build_prompt, ask_ai, WATCHLIST

# ===== 行情缓存 (5分钟有效) =====
import threading as _thr
_market_cache = {"data": None, "time": 0}
_market_lock = _thr.Lock()
def _get_market_overview():
    """并行获取行情概览，带5分钟缓存"""
    import time as _t
    now = _t.time()
    if _market_lock.acquire(timeout=10):
        try:
            if _market_cache["data"] and now - _market_cache["time"] < 300:
                return _market_cache["data"]
        finally:
            _market_lock.release()
    else:
        # Lock timeout - just return stale data or empty
        if _market_cache["data"]:
            return _market_cache["data"]
    # 并行获取
    from concurrent.futures import ThreadPoolExecutor as _TPE
    import json as _j
    cfg_path = os.path.expanduser("~/.hermes/user_watchlist.json")
    if os.path.exists(cfg_path):
        with open(cfg_path) as _f:
            wl = _j.load(_f)
        cur_wl = [(k, v) for k, v in wl.items()]
    else:
        cur_wl = [("BTC","btcusdt"),("ETH","ethusdt"),("SOL","solusdt"),("BNB","bnbusdt")]
    def _fetch_one(name, symbol):
        try:
            data, _ = analyze_symbol(name, symbol)
            if "error" not in data:
                return f"{name}: ${data['price']:,.2f} (24h: {data['change_24h']:+.2f}%, RSI: {data['rsi']})"
        except:
            pass
        return None
    with _TPE(max_workers=4) as pool:
        results = list(pool.map(lambda x: _fetch_one(*x), cur_wl))
    overview = "当前各币种行情:\n" + "\n".join(r for r in results if r)
    with _market_lock:
        _market_cache["data"] = overview
        _market_cache["time"] = now
        # Wake up any waiting threads
    return overview

from wechat_push import get_token, send_text


# ==================== 日志 ====================

def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{msg}\n")
    print(msg)


# ==================== AES 加解密 ====================

def decrypt_aes(encoding_aes_key, msg_encrypt):
    """解密企业微信消息"""
    aes_key = base64.b64decode(encoding_aes_key + "=")
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16]))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(base64.b64decode(msg_encrypt)) + decryptor.finalize()

    pad_len = decrypted[-1]
    content = decrypted[:-pad_len]

    msg_len = int.from_bytes(content[16:20], byteorder="big")
    msg = content[20:20+msg_len]
    receive_id = content[20+msg_len:].decode("utf-8")

    return msg.decode("utf-8"), receive_id


def encrypt_aes(encoding_aes_key, msg, receive_id):
    """加密回复消息"""
    import random
    aes_key = base64.b64decode(encoding_aes_key + "=")

    # 构建明文: [16字节随机][4字节长度][消息][企业ID]
    rand_str = "".join(chr(random.randint(65, 90)) for _ in range(16))
    msg_bytes = msg.encode("utf-8")
    msg_len = len(msg_bytes)
    raw = rand_str.encode() + msg_len.to_bytes(4, byteorder="big") + msg_bytes + receive_id.encode()

    # PKCS7 填充
    block_size = 32
    pad_len = block_size - (len(raw) % block_size)
    raw += bytes([pad_len] * pad_len)

    # AES 加密
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16]))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(raw) + encryptor.finalize()

    return base64.b64encode(encrypted).decode("utf-8")


def verify_signature(token, timestamp, nonce, echostr):
    sort_list = sorted([token, timestamp, nonce, echostr])
    sign_str = "".join(sort_list)
    return hashlib.sha1(sign_str.encode("utf-8")).hexdigest()


# ==================== 消息处理 ====================

def handle_message(content, from_user):
    """处理用户发来的消息，返回回复内容"""
    content = content.strip().lower()
    log(f"处理消息: '{content}' 来自: {from_user}")

    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")

    # 先尝试解析价格预警
    from price_alerts import parse_alert_from_message
    alert, alert_reply = parse_alert_from_message(content, from_user)
    if alert_reply:
        log(f"价格预警: {alert}")
        return alert_reply

    # 先尝试用 AI 理解意图
    try:
        # 动态加载当前监控列表
        import json
        cfg_path = os.path.expanduser("~/.hermes/user_watchlist.json")
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                wl = json.load(f)
            current_watchlist = [(k, v) for k, v in wl.items()]
        else:
            current_watchlist = [("BTC","btcusdt"),("ETH","ethusdt"),("SOL","solusdt"),("BNB","bnbusdt")]
        
        # Step 1: Keyword-based coin detection (fast, no AI call)
        matched_coin = None
        content_upper = content.upper()
        for c_name, c_sym in current_watchlist:
            if c_name in content_upper or c_sym.replace("usdt","").upper() in content_upper:
                matched_coin = c_name
                break
        
        # Step 2: Fetch data only for the relevant coin(s)
        if matched_coin:
            from crypto_monitor import analyze_symbol as _analyze
            data, _ = _analyze(matched_coin, next(s for c,s in current_watchlist if c == matched_coin))
            if "error" not in data:
                overview = "\u5f53\u524d\u5404\u5e01\u79cd\u884c\u60c5:\n{}: ${:,.2f} (24h: {:+.2f}%, RSI: {})".format(matched_coin, data["price"], data["change_24h"], data["rsi"])
            else:
                overview = "{} \u6570\u636e\u83b7\u53d6\u4e2d...".format(matched_coin)
        else:
            overview = _get_market_overview()
    except Exception as e:
        overview = "\u884c\u60c5\u6570\u636e\u6682\u65f6\u83b7\u53d6\u4e2d...\n"
        log("\u884c\u60c5\u83b7\u53d6\u5f02\u5e38: {}".format(e))

    # AI 理解用户意图
    try:
        ai_prompt = f"""你是一个币圈AI助手，说话风格亲切但专业。用户发了一条消息: "{content}"

当前行情 (唯一可信的数据源):
{overview}

铁律: 所有价格必须使用上面提供的实时数据，绝对不要自己编造。

回复要求:
- 如果用户是闲聊/打招呼 → 自然回复即可
- 如果用户问行情、分析、建议 → 直接分析回答
- 如果用户想查某个币 → 第一行写 QUERY:币种名(大写)，然后分析
- 如果用户想加币种监控 → 第一行写 ADD:币种名(大写)
- 如果用户想删币种监控 → 第一行写 REMOVE:币种名(大写)
- 如果用户想设置价格预警(如"LTC 46买"、"BTC 7万卖") → 第一行写 ALERT:币种:价格:方向(buy/sell)

回复自然一点，控制在200字内。如果回复包含多个币种的数据，用整齐的格式排列，例如：
BTC    $73,491   -4.64%  RSI 57.8
ETH    $2,019    -4.22%  RSI 58.0
方便用户一眼看清。"""

        reply = ask_ai(ai_prompt)
        log(f"AI回复: {reply[:200]}")

        # 解析 AI 回复中的指令
        lines = reply.strip().split("\n")
        first_line = lines[0].strip()

        if first_line.startswith("QUERY:"):
            coin = first_line.replace("QUERY:", "").strip()
            return handle_coin_query(coin, from_user)

        elif first_line.startswith("ADD:"):
            coin = first_line.replace("ADD:", "").strip()
            return handle_add_coin(coin, from_user)

        elif first_line.startswith("REMOVE:"):
            coin = first_line.replace("REMOVE:", "").strip()
            return handle_remove_coin(coin, from_user)

        elif first_line.startswith("ALERT:"):
            parts = first_line.replace("ALERT:", "").strip().split(":")
            if len(parts) >= 3:
                coin = parts[0].strip()
                try:
                    price = float(parts[1].strip())
                except ValueError:
                    price = 0
                direction = parts[2].strip().lower() if len(parts) >= 3 else "buy"
                if price > 0:
                    from price_alerts import add_alert
                    alert = add_alert(from_user, coin, price, direction, content)
                    return f"✅ 已设置 {coin} {'买入' if direction == 'buy' else '卖出'}预警！\n触发价: ${price:,.2f}\n到了我会通知你 ⏰"
            return reply

        else:
            # 纯文本回复
            return reply

    except Exception as e:
        log(f"AI理解失败: {e}")
        return '🤔 没听懂你的意思，试试发"帮助"看看能做什么'


def handle_coin_query(coin, user_id=None):
    """查询单个币种行情 - 详细版 (带走势图+AI分析)"""
    name = coin.upper()
    symbol = f"{coin.lower()}usdt"
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")

    try:
        data, chart_path = analyze_symbol(name, symbol)
        if "error" in data:
            return f"⚠️ {name} 暂时获取不到数据"

        # 分析文字
        analysis = ask_ai(build_prompt(data))

        # 组装回复
        reply = f"📊 {name} 行情分析  [{now_str}]\n\n"
        reply += analysis

        # 如果有走势图，后台推送图 + 文字
        if chart_path and user_id:
            import threading
            def push_with_chart():
                try:
                    from wechat_push import get_token, send_image, send_text
                    token = get_token()
                    time.sleep(0.5)
                    # 上传图片
                    import os
                    with open(chart_path, 'rb') as f:
                        import requests as rq
                        url = "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
                        resp = rq.post(url, params={"access_token": token, "type": "image"},
                                       files={"media": ("chart.png", f, "image/png")}, timeout=30)
                        mid = resp.json().get("media_id")
                    if mid:
                        # 发图
                        payload_img = {
                            "touser": user_id, "msgtype": "image", "agentid": AGENTID,
                            "image": {"media_id": mid}, "safe": 0
                        }
                        rq.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
                                params={"access_token": token}, json=payload_img, timeout=10)
                except Exception as e:
                    log(f"推送走势图异常: {e}")

            threading.Thread(target=push_with_chart, daemon=True).start()

        return reply

    except Exception as e:
        log(f"查询{name}异常: {e}")
        return f"⏳ {name} 数据加载中，稍等一下～"


# ==================== 加减币种 ====================

VALID_COINS = ["BTC", "ETH", "SOL", "BNB", "DOGE", "XRP", "ADA", "DOT", "AVAX", "LINK", "MATIC", "ATOM", "FIL", "TRX", "SHIB", "PEPE"]

def _get_watchlist_path():
    """获取监控列表配置文件路径"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crypto_monitor_config.json")

def _read_watchlist():
    """读取当前监控列表"""
    import json
    # 先从 crypto_monitor.py 的 WATCHLIST 获取
    # 实际用配置文件
    cfg_path = os.path.expanduser("~/.hermes/user_watchlist.json")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            return json.load(f)
    # 默认
    default = {
        "BTC": "btcusdt",
        "ETH": "ethusdt",
        "SOL": "solusdt",
        "BNB": "bnbusdt",
    }
    return default

def _save_watchlist(watchlist):
    """保存监控列表"""
    import json
    cfg_path = os.path.expanduser("~/.hermes/user_watchlist.json")
    with open(cfg_path, "w") as f:
        json.dump(watchlist, f, indent=2)
    # 也同步更新 crypto_monitor.py 的 WATCHLIST
    import crypto_monitor
    crypto_monitor.WATCHLIST = [(k, v) for k, v in watchlist.items()]

def handle_add_coin(coin, user_id):
    """添加币种到监控"""
    coin = coin.upper().strip()
    watchlist = _read_watchlist()
    
    if coin in watchlist:
        return f"⚠️ {coin} 已经在监控列表里了"
    
    symbol = f"{coin.lower()}usdt"
    watchlist[coin] = symbol
    _save_watchlist(watchlist)
    
    return f"✅ 已加入 {coin} 监控！\n当前监控: {', '.join(watchlist.keys())}"

def handle_remove_coin(coin, user_id):
    """从监控移除币种"""
    coin = coin.upper().strip()
    watchlist = _read_watchlist()
    
    if coin not in watchlist:
        return f"⚠️ {coin} 不在监控列表中"
    
    del watchlist[coin]
    _save_watchlist(watchlist)
    
    return f"✅ 已移除 {coin} 监控\n当前监控: {', '.join(watchlist.keys()) if watchlist else '无'}"


# ==================== 路由 ====================

@app.route("/", methods=["GET", "POST"])
def wechat_callback():
    if request.method == "GET":
        return handle_get()
    return handle_post()


def handle_get():
    """处理 URL 验证"""
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echostr = request.args.get("echostr", "")

    my_sign = verify_signature(TOKEN, timestamp, nonce, echostr)
    if my_sign != msg_signature:
        log(f"⚠️ 签名不匹配: 计算={my_sign[:16]}..., 期望={msg_signature[:16]}...")

    try:
        decrypted, receive_id = decrypt_aes(ENCODING_AES_KEY, echostr)
        log(f"URL验证成功")
        resp = make_response(decrypted)
        resp.headers["Content-Type"] = "text/plain"
        return resp
    except Exception as e:
        log(f"解密失败: {e}")
        resp = make_response(echostr)
        resp.headers["Content-Type"] = "text/plain"
        return resp


def handle_post():
    """处理用户发来的消息"""
    body = request.get_data(as_text=True)
    log(f"\n=== 收到POST消息 ===")
    log(f"Body: {body[:500]}")

    # 解析 XML
    try:
        root = ET.fromstring(body)

        # 获取加密信息
        encrypt_node = root.find("Encrypt")
        if encrypt_node is None:
            log("没有 Encrypt 节点")
            return make_response("ok")

        encrypt_xml = encrypt_node.text

        # 解密
        msg_text, receive_id = decrypt_aes(ENCODING_AES_KEY, encrypt_xml)
        log(f"解密消息: {msg_text}")

        # 解析解密后的 XML
        msg_root = ET.fromstring(msg_text)
        content = msg_root.find("Content")
        from_user = msg_root.find("FromUserName")

        if content is not None:
            user_msg = content.text
            user_id = from_user.text if from_user is not None else "unknown"
            log(f"用户 {user_id}: {user_msg}")

            # 处理消息（在后台线程执行，不影响回调响应）
            import threading
            def reply_later(uid, msg):
                try:
                    reply = handle_message(msg, uid)
                    log(f"回复: {reply}")
                    time.sleep(1)
                    # 主动推送给用户
                    push_result = push_to_user(uid, reply)
                    log(f"推送结果: {push_result}")
                except Exception as e:
                    log(f"回复推送异常: {e}")

            t = threading.Thread(target=reply_later, args=(user_id, user_msg))
            t.daemon = True
            t.start()

            # 立即返回 ok（企业微信收到空响应表示消息已接收）
            resp = make_response("ok")
            resp.headers["Content-Type"] = "text/plain"
            return resp

    except Exception as e:
        log(f"处理POST异常: {e}")

    return make_response("ok")


@app.route("/health", methods=["GET"])
def health():
    return "ok"


# ==================== 消息推送工具 ====================

def push_to_user(user_id, text):
    """主动推消息给指定用户 (自动加上时间戳)"""
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")
    text = f"⏰ {now_str}\n{text}"
    try:
        token = get_token()
        url = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": AGENTID,
            "text": {"content": text},
            "safe": 0
        }
        resp = requests.post(url, params={"access_token": token},
                             json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        log(f"主动推送失败: {e}")
        return None


if __name__ == "__main__":
    log("=" * 50)
    log("企业微信回调服务 (带对话功能)")
    log("=" * 50)

    app.run(host="0.0.0.0", port=8080, debug=False)
