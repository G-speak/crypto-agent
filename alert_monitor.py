#!/usr/bin/env python3
"""
实时异动监控模块
价格波动、RSI超卖超买、布林带突破自动推送
"""
import os, sys, time, json
import traceback
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(__file__))

# ====== 配置 ======
# 主流币种 (波动阈值3%)
MAJOR_COINS = ["BTC", "ETH"]
# 小币种 (波动阈值7%)
# 其余币种都算小币种

ALERT_COOLDOWN = 1800  # 同一币种同一类型预警最少间隔30分钟
MONITOR_INTERVAL = 300  # 检查间隔 (5分钟)

LOG_FILE = os.path.expanduser("~/.hermes/logs/alert_monitor.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
STATE_FILE = os.path.expanduser("~/.hermes/alert_state.json")
COOLDOWN_FILE = os.path.expanduser("~/.hermes/alert_cooldown.json")

def log(msg):
    t = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{t}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)

def load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

def is_on_cooldown(coin, alert_type, cooldown):
    key = f"{coin}:{alert_type}"
    now = time.time()
    if key in cooldown and now - cooldown[key] < ALERT_COOLDOWN:
        return True
    cooldown[key] = now
    return False

def check_alerts():
    from crypto_monitor import WATCHLIST, analyze_symbol

    state = load_json(STATE_FILE)
    cooldown = load_json(COOLDOWN_FILE)
    alerts = []
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")

    for coin_name, coin_symbol in WATCHLIST:
        try:
            data, chart = analyze_symbol(coin_name, coin_symbol)
            if "error" in data:
                continue

            price = data["price"]
            change = data.get("change_24h", 0)
            rsi = data.get("rsi", 50)
            bb_l = data.get("bb_lower", 0)
            bb_u = data.get("bb_upper", 0)
            is_major = coin_name in MAJOR_COINS
            vol_threshold = 3.0 if is_major else 7.0

            prev = state.get(coin_name, {})

            # 1. 价格波动预警 (相对上次检查)
            if prev and "price" in prev and prev["price"] > 0:
                pct = abs(price - prev["price"]) / prev["price"] * 100
                if pct >= vol_threshold:
                    direction = "上涨" if price > prev["price"] else "下跌"
                    if not is_on_cooldown(coin_name, "price_spike", cooldown):
                        alerts.append(
                            f"🚨 {coin_name} 价格异动\n"
                            f"当前: ${price:,.2f}\n"
                            f"24h: {change:+.2f}%\n"
                            f"较上次: {direction} {pct:.1f}%\n"
                            f"RSI: {rsi:.1f}\n"
                            f"⏰ {now_str}"
                        )

            # 2. RSI超卖/超买
            if rsi <= 25 and not is_on_cooldown(coin_name, "rsi_oversold", cooldown):
                alerts.append(
                    f"⚠️ {coin_name} RSI超卖\n"
                    f"价: ${price:,.2f}  RSI: {rsi:.1f}\n"
                    f"24h: {change:+.2f}%\n"
                    f"短期可能反弹，注意风险\n"
                    f"⏰ {now_str}"
                )
            elif rsi >= 75 and not is_on_cooldown(coin_name, "rsi_overbought", cooldown):
                alerts.append(
                    f"⚠️ {coin_name} RSI超买\n"
                    f"价: ${price:,.2f}  RSI: {rsi:.1f}\n"
                    f"24h: {change:+.2f}%\n"
                    f"注意回调风险\n"
                    f"⏰ {now_str}"
                )

            # 3. 布林带突破
            if bb_l > 0 and bb_u > 0:
                if price <= bb_l and not is_on_cooldown(coin_name, "bb_lower", cooldown):
                    alerts.append(
                        f"📉 {coin_name} 跌破布林下轨\n"
                        f"价: ${price:,.2f}  下轨: ${bb_l:,.2f}\n"
                        f"RSI: {rsi:.1f}  24h: {change:+.2f}%\n"
                        f"⏰ {now_str}"
                    )
                elif price >= bb_u and not is_on_cooldown(coin_name, "bb_upper", cooldown):
                    alerts.append(
                        f"📈 {coin_name} 涨破布林上轨\n"
                        f"价: ${price:,.2f}  上轨: ${bb_u:,.2f}\n"
                        f"RSI: {rsi:.1f}  24h: {change:+.2f}%\n"
                        f"⏰ {now_str}"
                    )

            state[coin_name] = {"price": price, "time": time.time()}

        except Exception as e:
            log(f"检查 {coin_name} 出错: {e}")
            continue

    save_json(STATE_FILE, state)
    save_json(COOLDOWN_FILE, cooldown)
    return alerts

def push_alerts(alerts):
    if not alerts:
        return
    from crypto_monitor import build_prompt, ask_ai
    from wechat_push import send_simple_message
    import requests as _req, json as _json
    
    # 提取触发了预警的币种名
    triggered_coins = set()
    for a in alerts:
        for coin_name in MAJOR_COINS:
            if coin_name in a:
                triggered_coins.add(coin_name)
                break
    from crypto_monitor import WATCHLIST
    for c_name, _ in WATCHLIST:
        for a in alerts:
            if c_name in a:
                triggered_coins.add(c_name)
    
    extra_text = ""
# 为每个预警币种生成简短AI分析
    for coin_name in triggered_coins:
        try:
            from crypto_monitor import analyze_symbol
            data, _ = analyze_symbol(coin_name, next(s for c,s in WATCHLIST if c == coin_name))
            if "error" not in data:
                prompt = build_prompt(data)
                short_prompt = "请用1-2句话简要分析这个币种的行情，包括价格、RSI和操作建议。控制在80字内。\n\n" + prompt
                reply = ask_ai(short_prompt, model="deepseek-chat")
                # 跳过标题行（📊🎯⚠️开头），取实际分析内容
                text = reply.strip()
                lines = text.split("\n")
                start = 0
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("📊") and not stripped.startswith("🎯") and not stripped.startswith("⚠️"):
                        start = i
                        break
                short_text = "\n".join(lines[start:]).strip()
                short_reply = short_text[:150] if short_text else text[:150]
                extra_text += f"\n📊 {coin_name} 简析: {short_reply}"
        except Exception as e:
            log(f"分析 {coin_name} 失败: {e}")
        

    # 新闻搜索：按币种+按天缓存，每个币今天只搜一次
    _news_cache_file = os.path.expanduser("~/.hermes/news_cache.json")
    _news_cache = {}
    if os.path.exists(_news_cache_file):
        try:
            with open(_news_cache_file) as _f:
                _news_cache = json.load(_f)
        except:
            pass
    _today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    # 找出今天还没搜过的币
    coins_to_search = []
    for coin in triggered_coins:
        cache_key = f"news_{coin}_{_today}"
        if cache_key not in _news_cache:
            coins_to_search.append(coin)
        else:
            cached = _news_cache[cache_key].get("text", "")
            if cached:
                clean_cached = cached.replace("**", "")
                extra_text += f"\n\n📰 {coin} 消息面:\n{clean_cached}"
    if coins_to_search:
        try:
            from wechat_config import AI_API_KEY
            APP_CODE = os.environ.get("AIHUBMIX_APP_CODE", "")
            coin_names = "、".join(coins_to_search)
            news_prompt = f"现在是2026年6月。请搜索{coin_names}和加密货币市场今天的最新新闻，列出2-3条最重要的具体事件（含来源）。必须搜索实时新闻。"
            news_headers = {
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json"
            }
            if APP_CODE:
                news_headers["APP-Code"] = APP_CODE
            news_resp = _req.post(
                "https://api.aihubmix.com/v1/chat/completions",
                headers=news_headers,
                json={"model": "gpt-4o-mini-search-preview", "messages": [{"role": "user", "content": news_prompt}]},
                timeout=25
            )
            if news_resp.status_code == 200:
                news = news_resp.json()["choices"][0]["message"]["content"]
                clean_news = news.replace("**", "")
                extra_text += f"\n\n📰 {coin_names} 消息面:\n{clean_news}"
                _news_cache[f"news_{coin}_{_today}"] = {"text": news, "time": time.time()}
                with open(_news_cache_file, "w") as _f:
                    json.dump(_news_cache, _f)
                log(f"已搜索并缓存{coin_names}的新闻")
            else:
                log(f"新闻搜索失败: {news_resp.status_code}")
        except Exception as e:
            log(f"新闻搜索异常: {e}")

    # 推送: 预警 + AI简析 + 消息面
    for i in range(0, len(alerts), 5):
        msg = "🔔 实时监控\n" + "\n\n".join(alerts[i:i+5])
        if extra_text:
            msg += "\n" + "-" * 20 + extra_text
        try:
            send_simple_message(msg)
            log(f"已推送 {len(alerts[i:i+5])} 条预警 (含AI分析+消息面)")
            time.sleep(1)
        except Exception as e:
            log(f"推送失败: {e}")

def monitor_loop():
    log("实时异动监控已启动")
    log(f"主流币(3%阈值): {MAJOR_COINS}")
    log(f"检查间隔: {MONITOR_INTERVAL}秒")
    while True:
        try:
            alerts = check_alerts()
            if alerts:
                log(f"发现 {len(alerts)} 条预警")
                push_alerts(alerts)
            time.sleep(MONITOR_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"异常: {e}\n{traceback.format_exc()}")
            time.sleep(60)

if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        log("=== 单次检查 ===")
        alerts = check_alerts()
        if alerts:
            for a in alerts:
                print(a)
                print("---")
            push_alerts(alerts)
        else:
            log("本轮无异动")
    else:
        monitor_loop()
