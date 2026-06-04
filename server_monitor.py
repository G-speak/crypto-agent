#!/usr/bin/env python3
"""
服务器监控启动脚本
启动: 价格预警检查 + 监控循环
"""
import os
import sys
import threading

# 设置 DeepSeek API Key
import wechat_config
os.environ["DEEPSEEK_API_KEY"] = wechat_config.DEEPSEEK_API_KEY

sys.path.insert(0, "/root/crypto_agent")

# 启动预警检查线程
print("启动预警检查...")
import price_alerts
from crypto_monitor import analyze_symbol

def get_price_for_alert(coin):
    """获取币种当前价格"""
    symbol_map = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt", "BNB": "bnbusdt",
                  "LTC": "ltcusdt", "DOGE": "dogeusdt", "XRP": "xrpusdt", "ADA": "adausdt"}
    symbol = symbol_map.get(coin.upper())
    if not symbol:
        return None
    try:
        data, _ = analyze_symbol(coin, symbol)
        return data.get("price")
    except Exception:
        return None

t = threading.Thread(target=price_alerts.alert_checker, args=(get_price_for_alert,), daemon=True)
t.start()
print("预警检查已启动 ✅")

# 启动监控循环
print("启动监控循环...")
from run_monitor import monitor_loop
monitor_loop()
