#!/usr/bin/env python3
"""
价格预警系统
用户设置价格提醒，到了自动推送
"""

import os
import json
import time
import threading
from datetime import datetime, timezone, timedelta

CONFIG_DIR = os.path.expanduser("~/.hermes/price_alerts")
os.makedirs(CONFIG_DIR, exist_ok=True)


def get_user_file(user_id):
    """获取用户预警文件路径"""
    safe_name = user_id.replace("@", "_").replace(".", "_")
    return os.path.join(CONFIG_DIR, f"{safe_name}.json")


def load_alerts(user_id):
    """加载用户的价格预警列表"""
    fpath = get_user_file(user_id)
    if os.path.exists(fpath):
        with open(fpath) as f:
            return json.load(f)
    return []


def save_alerts(user_id, alerts):
    """保存用户的价格预警列表"""
    fpath = get_user_file(user_id)
    with open(fpath, "w") as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)


def add_alert(user_id, coin, price, alert_type, note=""):
    """
    添加价格预警
    coin: BTC/ETH/SOL/BNB
    price: 触发价格
    alert_type: "buy" 或 "sell"
    note: 用户备注
    """
    alerts = load_alerts(user_id)

    alert = {
        "id": len(alerts) + 1,
        "coin": coin.upper(),
        "price": price,
        "type": alert_type,
        "note": note,
        "created_at": datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M"),
        "triggered": False
    }

    alerts.append(alert)
    save_alerts(user_id, alerts)
    return alert


def remove_alert(user_id, alert_id):
    """删除预警"""
    alerts = load_alerts(user_id)
    alerts = [a for a in alerts if a["id"] != alert_id]
    save_alerts(user_id, alerts)
    return len(alerts) < len(load_alerts(user_id))  # 如果删了返回True


def list_alerts(user_id):
    """列出用户所有未触发的预警"""
    alerts = load_alerts(user_id)
    active = [a for a in alerts if not a["triggered"]]
    return active


# 存储所有用户的预警，用于监控线程
_all_alerts_cache = {}
_last_cache_time = 0


def get_all_active_alerts():
    """获取所有用户未触发的预警"""
    global _all_alerts_cache, _last_cache_time

    now = time.time()
    if now - _last_cache_time < 10:
        return _all_alerts_cache

    all_alerts = {}
    if os.path.exists(CONFIG_DIR):
        for fname in os.listdir(CONFIG_DIR):
            if fname.endswith(".json"):
                user_id = fname.replace(".json", "").replace("_", "@").replace("@", ".")
                fpath = os.path.join(CONFIG_DIR, fname)
                try:
                    with open(fpath) as f:
                        alerts = json.load(f)
                    active = [a for a in alerts if not a["triggered"]]
                    if active:
                        all_alerts[user_id] = active
                except Exception:
                    pass

    _all_alerts_cache = all_alerts
    _last_cache_time = now
    return all_alerts


def mark_triggered(user_id, alert_id):
    """标记预警已触发"""
    alerts = load_alerts(user_id)
    for a in alerts:
        if a["id"] == alert_id:
            a["triggered"] = True
            a["triggered_at"] = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
            break
    save_alerts(user_id, alerts)


# ==================== AI 解析用户消息中的预警 ====================

def parse_alert_from_message(content, user_id):
    """
    解析用户消息，看是否有设置价格预警
    返回: (alert_info, reply_text) 或 (None, None)
    """
    content_lower = content.lower().strip()

    # 识别币种
    coin_map = {
        "btc": "BTC", "比特币": "BTC", "大饼": "BTC",
        "eth": "ETH", "以太坊": "ETH", "以太": "ETH",
        "sol": "SOL", "solana": "SOL",
        "bnb": "BNB", "币安币": "BNB",
        "ltc": "LTC", "莱特币": "LTC", "莱特": "LTC",
        "doge": "DOGE", "狗狗币": "DOGE",
        "xrp": "XRP", "瑞波": "XRP",
        "ada": "ADA", "艾达": "ADA",
        "dot": "DOT", "波卡": "DOT",
        "link": "LINK", "chainlink": "LINK",
    }

    target_coin = None
    for key, coin in coin_map.items():
        if key in content_lower:
            target_coin = coin
            break

    if not target_coin:
        return None, None

    # 识别方向 (买/卖)
    is_buy = any(w in content_lower for w in ["买", "入", "抄底", "建仓"])
    is_sell = any(w in content_lower for w in ["卖", "出", "止盈", "止损"])

    if not is_buy and not is_sell:
        return None, None

    alert_type = "buy" if is_buy else "sell"
    type_label = "买入" if is_buy else "卖出"

    # 提取价格
    import re
    prices = re.findall(r'(\d+\.?\d*)', content)
    target_price = None
    for p in prices:
        val = float(p)
        # 合理的价格范围
        if target_coin == "BTC" and 10000 < val < 200000:
            target_price = val
            break
        elif target_coin == "ETH" and 500 < val < 10000:
            target_price = val
            break
        elif target_coin == "SOL" and 10 < val < 500:
            target_price = val
            break
        elif target_coin == "BNB" and 100 < val < 2000:
            target_price = val
            break
        elif target_coin == "LTC" and 20 < val < 200:
            target_price = val
            break
        elif target_coin == "DOGE" and 0.01 < val < 1:
            target_price = val
            break
        elif target_coin == "XRP" and 0.1 < val < 5:
            target_price = val
            break

    if not target_price:
        return None, None

    # 添加预警
    alert = add_alert(user_id, target_coin, target_price, alert_type, content)

    reply = (
        f"✅ 已设置 {target_coin} {type_label}预警！\n"
        f"📌 触发价: ${target_price:,.2f}\n"
        f"等价格到了我会第一时间通知你 ⏰\n\n"
        f"如需取消，请说\"取消预警\""
    )

    return alert, reply


# ==================== 预警检查线程 ====================

def alert_checker(get_price_func):
    """
    预警检查线程
    get_price_func: 函数(coin) -> 当前价格
    """
    from wechat_push import send_simple_message

    while True:
        try:
            all_alerts = get_all_active_alerts()
            if not all_alerts:
                time.sleep(30)
                continue

            for user_id, alerts in all_alerts.items():
                for alert in alerts:
                    coin = alert["coin"]
                    target_price = alert["price"]
                    alert_type = alert["type"]
                    direction = "买入" if alert_type == "buy" else "卖出"

                    # 获取当前价格
                    current_price = get_price_func(coin)
                    if not current_price:
                        continue

                    # 检查是否触发
                    triggered = False
                    if alert_type == "buy" and current_price <= target_price * 1.01:
                        triggered = True
                    elif alert_type == "sell" and current_price >= target_price * 0.99:
                        triggered = True

                    if triggered:
                        msg = (
                            f"⏰ 价格预警触发！\n"
                            f"{coin} 已达到您设置的 {direction}位 ${target_price:,.2f}\n"
                            f"当前价格: ${current_price:,.2f}\n"
                            f"建议关注行情，按计划操作 ⚡"
                        )
                        send_simple_message(msg)
                        mark_triggered(user_id, alert["id"])

            time.sleep(30)

        except Exception as e:
            print(f"预警检查异常: {e}")
            time.sleep(30)
