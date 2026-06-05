#!/usr/bin/env python3
"""
每日市场要闻 - 每天早上09:00推送
包含：宏观事件、大户动向、主流币新闻
测试模式：--dry-run 只打印不推送
"""
import os
import sys
import json
import time
import requests
from datetime import datetime, timezone, timedelta

# 加入上级目录，复用现有配置和函数
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ====== 配置 ======
LOG_FILE = os.path.expanduser("~/.hermes/logs/daily_news.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
NEWS_DAILY_FILE = os.path.expanduser("~/.hermes/news_daily.json")

MAJOR_COINS = ["BTC", "ETH"]

def log(msg):
    t = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{t}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)

def search_news(query, api_key, app_code=""):
    """搜索新闻，返回文本内容"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    if app_code:
        headers["APP-Code"] = app_code
    
    resp = requests.post(
        "https://api.aihubmix.com/v1/chat/completions",
        headers=headers,
        json={
            "model": "gpt-4o-mini-search-preview",
            "messages": [{"role": "user", "content": query}]
        },
        timeout=30
    )
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]
    else:
        log(f"搜索失败: {resp.status_code} {resp.text[:200]}")
        return ""

def generate_daily_news(dry_run=True):
    """生成每日市场要闻"""
    from wechat_config import AI_API_KEY
    APP_CODE = os.environ.get("AIHUBMIX_APP_CODE", "TWGT4339")
    
    today = datetime.now(timezone(timedelta(hours=8)))
    date_str = today.strftime("%Y年%m月%d日")
    
    log(f"=== 每日新闻生成 ({date_str}) ===")
    
    # 一次搜索，涵盖所有内容
    query = (
        f"现在是{date_str}。请搜索今天加密货币市场最重要的新闻事件，包含："
        f"1) 宏观政策/监管动态 "
        f"2) 知名人物（马斯克、贝莱德、Strategy等）相关动态 "
        f"3) BTC/ETH等主流币的重大价格变动或链上事件。"
        f"请直接列出3-5条最重要的新闻，每条一句完整的话（30-50字），包含来源名称。"
        f"格式要求：每条单独一行，不要序号，不要空行，不要任何额外说明。"
        f"注意：只列真正的新闻事件，不要列出股票行情、价格数据、市场综述类信息。"
    )
    news = search_news(query, AI_API_KEY, APP_CODE)
    news = news.replace("**", "").strip()
    
    if not news:
        log("未获取到新闻内容")
        return
    
    # 清理：去掉股票行情行（包含"在 CRYPTO 市场中是crypto"、"价格为"、"当日最高价"等）
    lines = news.split("\n")
    clean_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "在 CRYPTO 市场中" in line:
            continue
        if line.startswith("价格为") or line.startswith("当日最高价") or line.startswith("当日最低价"):
            continue
        if "价格为" in line and "USD" in line:
            continue
        clean_lines.append(line)
    
    # 编号
    numbered_items = []
    for line in clean_lines:
        if len(line) < 10:
            continue
        # 去掉开头的数字/符号
        cleaned = line.lstrip("0123456789.-•· ")
        if cleaned:
            numbered_items.append(f"#{len(numbered_items)+1} {cleaned}")
    
    if not numbered_items:
        log("清理后无有效新闻")
        return
    
    msg = f"📰 今日市场要闻\n{date_str}\n\n"
    msg += "\n".join(numbered_items)
    msg += "\n\n⚠️ 以上信息由AI搜索整理，仅供参考。"
    
    log(f"共 {len(numbered_items)} 条有效新闻")
    for item in numbered_items:
        log(f"  {item[:80]}")
    
    if dry_run:
        log("=== DRY RUN 模式，不推送 ===")
        print("\n" + "="*40)
        print(msg)
        print("="*40 + "\n")
        return
    
    # 推送到企业微信
    from wechat_push import send_simple_message
    ok = send_simple_message(msg)
    if ok:
        log("每日新闻推送成功")
        # 写入缓存
        items_data = []
        for item in numbered_items:
            items_data.append({"id": item.split(" ")[0].lstrip("#"), "summary": item})
        cache_data = {
            "items": items_data,
            "pushed_at": time.time(),
            "date": date_str
        }
        with open(NEWS_DAILY_FILE, "w") as f:
            json.dump(cache_data, f)
        log("已写入 news_daily.json 缓存")
    else:
        log("每日新闻推送失败")

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    generate_daily_news(dry_run=dry_run)
