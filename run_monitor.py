#!/usr/bin/env python3
import os, sys, json, time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crypto_monitor import WATCHLIST, analyze_symbol, build_prompt, ask_ai
from wechat_push import push_analysis
from wechat_config import PUSH_TIMES, CLIENT_NAME
LOG_FILE = os.path.expanduser(f"~/.hermes/logs/crypto_monitor_{CLIENT_NAME}.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
def log(msg):
    t = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, 'a') as f: f.write(str('[{}] {}').format(t, msg) + chr(10))
    print('[{}] {}'.format(t, msg))

# ── 账本战报推送 ──
_LEDGER_PUSHED_TODAY = False
_LEDGER_PUSH_DATE = ""

def push_ledger_report():
    """生成并推送今日模拟盘战报"""
    try:
        from clients.gateio_trade import generate_ledger_summary, _load_ledger
        ledger = _load_ledger()

        # 检查今天是否有交易
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        today_trades = [e for e in ledger if e.get("time", "").startswith(today)]

        summary = generate_ledger_summary()

        if today_trades:
            # 附加今日交易明细
            today_buy = sum(1 for e in today_trades if e.get("action") == "BUY")
            today_sell = sum(1 for e in today_trades if e.get("action") == "SELL")
            today_pnl = sum(e.get("pnl_usdt", 0) for e in today_trades if e.get("action") == "SELL")
            sign = "+" if today_pnl >= 0 else ""
            summary += (
                f"\n\n📋 今日交易简报\n"
                f"买入 {today_buy} 次 | 卖出 {today_sell} 次\n"
                f"今日盈亏: {sign}{today_pnl:.2f} USDT"
            )
        else:
            summary += "\n\n📋 今日无交易触发"

        from wechat_push import send_simple_message
        send_simple_message(summary)
        log("已推送 24:00 模拟盘战报")
    except Exception as e:
        log(f"账本战报推送异常: {e}")

def do_scheduled_push():
    log('定时推送进行中...')
    texts = []
    for name, symbol in WATCHLIST:
        data, _ = analyze_symbol(name, symbol)
        if 'error' in data: continue
        try:
            time.sleep(15)
            texts.append(ask_ai(build_prompt(data), model="auto"))
        except:
            texts.append('分析失败: {}'.format(name))
    push_analysis(texts, [])
    log('定时推送完成')

def monitor_loop():
    log('监控循环已启动')
    pushed_times = []
    global _LEDGER_PUSHED_TODAY, _LEDGER_PUSH_DATE
    while True:
        try:
            now = datetime.now(timezone(timedelta(hours=8)))
            now_str = now.strftime('%H:%M')
            today = now.strftime('%Y-%m-%d')

            # 重置每日战报标记
            if today != _LEDGER_PUSH_DATE:
                _LEDGER_PUSHED_TODAY = False
                _LEDGER_PUSH_DATE = today

            if pushed_times and today != pushed_times[0]: pushed_times = []
            for pt in PUSH_TIMES:
                if now_str == pt and pt not in pushed_times:
                    log('定时推送 ({})'.format(pt))
                    do_scheduled_push()
                    pushed_times.append(pt)
                    pushed_times.insert(0, today)

            # ── 每日 24:00（午夜）战报 ──
            if now_str == "00:00" and not _LEDGER_PUSHED_TODAY:
                push_ledger_report()
                _LEDGER_PUSHED_TODAY = True

            time.sleep(30)
        except Exception as e:
            log('异常: {}'.format(e))
            time.sleep(60)

def main():
    log('监控助手 v3.0 启动（含模拟盘战报）')
    monitor_loop()

if __name__ == '__main__':
    main()
