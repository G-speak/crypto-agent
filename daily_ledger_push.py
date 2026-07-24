#!/usr/bin/env python3
"""每日0点账本推送脚本"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clients.gateio_trade import generate_ledger_summary
from wechat_push import send_simple_message

text = "📅 每日账本\n" + generate_ledger_summary()
send_simple_message(text)
print("每日账本已推送")
