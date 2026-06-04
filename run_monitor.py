#!/usr/bin/env python3
import os, sys, json, time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/root/crypto_agent')
from crypto_monitor import WATCHLIST, analyze_symbol, build_prompt, ask_ai
from wechat_push import push_analysis
CONFIG_FILE = os.path.expanduser("~/.hermes/crypto_monitor_config.json")
DEFAULT_CONFIG = {'watchlist':['BTC','ETH','SOL','BNB'],'monitor_interval':300,'push_times':['08:00','20:00']}
LOG_FILE = os.path.expanduser("~/.hermes/logs/crypto_monitor.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
def log(msg):
    t = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, 'a') as f: f.write(str('[{}] {}').format(t, msg) + chr(10))
    print('[{}] {}'.format(t, msg))
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f: return json.load(f)
    return dict(DEFAULT_CONFIG)
def do_scheduled_push():
    log('定时推送进行中...')
    texts, charts = [], []
    for name, symbol in WATCHLIST:
        data, chart_path = analyze_symbol(name, symbol)
        if 'error' in data: continue
        charts.append(chart_path)
        try:
            texts.append(ask_ai(build_prompt(data)))
        except:
            texts.append('分析失败: {}'.format(name))
    push_analysis(texts, charts)
    log('定时推送完成')
def monitor_loop():
    cfg = load_config()
    log('监控循环已启动')
    pushed_times = []
    while True:
        try:
            now = datetime.now(timezone(timedelta(hours=8)))
            now_str = now.strftime('%H:%M')
            today = now.strftime('%Y-%m-%d')
            if pushed_times and today != pushed_times[0]: pushed_times = []
            for pt in cfg.get('push_times', ['08:00','20:00']):
                if now_str == pt and pt not in pushed_times:
                    log('定时推送 ({})'.format(pt))
                    do_scheduled_push()
                    pushed_times.append(pt)
                    pushed_times.insert(0, today)
            time.sleep(30)
        except Exception as e:
            log('异常: {}'.format(e))
            time.sleep(60)
def main():
    log('监控助手 v3.0 启动')
    monitor_loop()
if __name__ == '__main__':
    main()
