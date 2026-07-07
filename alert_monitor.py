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
from wechat_config import MAJOR_COINS, ALERT_COOLDOWN, MONITOR_INTERVAL, CLIENT_NAME

LOG_FILE = os.path.expanduser(f"~/.hermes/logs/alert_monitor_{CLIENT_NAME}.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
STATE_FILE = os.path.expanduser(f"~/.hermes/alert_state_{CLIENT_NAME}.json")
COOLDOWN_FILE = os.path.expanduser(f"~/.hermes/alert_cooldown_{CLIENT_NAME}.json")

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
    from crypto_monitor import build_prompt, ask_ai, WATCHLIST
    from wechat_push import send_simple_message
    import requests as _req, json as _json
    import hashlib
    from collections import defaultdict

    # 按币种分组预警
    coin_alerts = defaultdict(list)
    for a in alerts:
        found = False
        for coin_name in MAJOR_COINS:
            if coin_name in a:
                coin_alerts[coin_name].append(a)
                found = True
                break
        if not found:
            for c_name, _ in WATCHLIST:
                if c_name in a:
                    coin_alerts[c_name].append(a)
                    break

    _news_cache_file = os.path.expanduser("~/.hermes/news_cache.json")
    _news_cache = {}
    if os.path.exists(_news_cache_file):
        try:
            with open(_news_cache_file) as _f:
                _news_cache = json.load(_f)
        except:
            pass
    _today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")

    # 今天已推送过的新闻内容指纹
    _seen_news = set()
    _seen_cache_key = f"seen_news_{_today}"
    if _seen_cache_key in _news_cache:
        _seen_news = set(_news_cache[_seen_cache_key].get("fingerprints", []))

    for coin_name in coin_alerts:
        try:
            # AI简析 + JSON决策
            from crypto_monitor import analyze_symbol
            symbol = next(s for c,s in WATCHLIST if c == coin_name)
            data, _ = analyze_symbol(coin_name, symbol)
            ai_text = ""
            news_text = ""
            decision_text = ""
            if "error" not in data:
                # ── 获取新闻（先搜，让决策能看到消息面）──
                news_text = _fetch_coin_news(coin_name, _news_cache, _news_cache_file, _today)

                # ── 新版：文字状态 + JSON 决策 ──
                from clients.json_prompt import build_json_prompt, ask_ai_json
                decision = ask_ai_json(build_json_prompt(data, coin_name, news_text))
                action = decision.get("action", "HOLD")
                reason = decision.get("reason", "")

                # ===== 雷达拦截：BUY/SELL 唤醒 7 角色委员会 =====
                if action in ["BUY", "SELL"]:
                    action_cn = "买入" if action == "BUY" else "卖出"
                    action_emoji_signal = chr(0x1f7e2) if action == "BUY" else chr(0x1f534)
                    alert_msg = (
                        f"{chr(0x1f6a8)} 【雷达触发】发现 {coin_name} 潜在 {action_cn} 机会 {action_emoji_signal}\n"
                        f"浅层初筛理由: {reason}\n"
                        f"----------------------\n"
                        f"{chr(0x1f575)}{chr(0x200d)}{chr(0x2642)}{chr(0xfe0f)} 信号已捕获！正在强制唤醒 7 角色投研委员会进行深度评估..."
                    )
                    send_simple_message(alert_msg)
                    log(f"触发浅层 {action} 信号，正在唤醒 7 角色")

                    try:
                        import seven_roles_committee
                        seven_roles_committee.run_committee(coin_name, symbol, reason)
                    except Exception as e:
                        err_msg = f"{chr(0x26a0)}{chr(0xfe0f)} 7角色深度投研唤醒失败: {e}"
                        log(err_msg)
                        send_simple_message(err_msg)

                    # 7角色接管，跳过下方原有的下单和推送逻辑
                    continue
                # =======================================================

                # 交易信号推送给用户
                action_emoji = {"BUY": chr(0x1f7e2) + " 买入信号", "SELL": chr(0x1f534) + " 卖出信号", "HOLD": chr(0x26aa) + " 持有观望"}
                dr = get_dry_run_status()
                dry_note = f"\n{chr(0x26a0)}{chr(0xfe0f)} DRY RUN 模式，不会真实下单" if dr else ""

                # ── 执行下单（DRY_RUN 保护），捕获返回值用于 PnL ──
                trade_result = _execute_trade(coin_name, symbol, action)

                # 提取 PnL（仅 SELL + 有盈亏时）
                pnl_str = ""
                if action == "SELL" and trade_result:
                    pnl_pct = trade_result.get("pnl_pct", 0)
                    pnl_usdt = trade_result.get("pnl_usdt", 0)
                    if pnl_pct != 0:
                        sign = "+" if pnl_pct >= 0 else ""
                        direction = "赚" if pnl_pct >= 0 else "亏"
                        pnl_str = f"\n{chr(0x1f9f8)} 模拟平仓收益: {sign}{pnl_pct}% (大约{direction} {abs(pnl_usdt):.2f} USDT)"

                decision_text = (
                    f"---\n"
                    f"{chr(0x1f4a4)} {coin_name} 交易决策\n"
                    f"{action_emoji.get(action, action)}\n"
                    f"理由: {reason}{dry_note}{pnl_str}"
                )

                # ── 旧版简析保留（显示技术摘要给妈妈看）──
                prompt = build_prompt(data)
                short_prompt = "请用1-2句话简要分析这个币种的行情，包括价格、RSI和操作建议。控制在80字内。\n\n" + prompt
                reply = ask_ai(short_prompt, model="auto")
                text = reply.strip()
                lines = text.split("\n")
                start = 0
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("\xf0") and not stripped.startswith("\u26a0"):
                        start = i
                        break
                short_text = "\n".join(lines[start:]).strip()
                short_reply = short_text if short_text else text
                ai_text = short_reply

            # 新闻内容去重（_fetch_coin_news 已在决策前完成搜索和缓存）
            if news_text:
                fp = hashlib.md5(news_text.encode()).hexdigest()
                if fp in _seen_news:
                    news_text = ""  # 今天已经在其他币种消息中出现过，跳过
                else:
                    _seen_news.add(fp)
                    _news_cache[_seen_cache_key] = {"fingerprints": list(_seen_news), "time": time.time()}
                    with open(_news_cache_file, "w") as _f:
                        json.dump(_news_cache, _f)

            # 组装消息
            msg_parts = []
            # 预警内容
            coin_alert_text = "\n\n".join(coin_alerts[coin_name])
            msg_parts.append(coin_alert_text)

            # 分隔线 + AI简析
            if ai_text:
                msg_parts.append("---")
                msg_parts.append(f"\U0001f4ca {coin_name} 简析\n{ai_text}")

            # 交易决策
            if decision_text:
                msg_parts.append(decision_text)

            # 新闻
            if news_text:
                msg_parts.append("---")
                msg_parts.append(f"\U0001f4b0 消息面\n{news_text}")

            # 风控
            msg_parts.append("---")
            msg_parts.append("\u26a0\ufe0f 风控提示\n以上分析仅供参考，不构成投资建议。请自行判断风险。")

            msg = "\n".join(msg_parts)
            send_simple_message(msg)
            log(f"已推送 {coin_name} 预警 (含AI分析+消息面)")
            time.sleep(3)  # 排队削峰：每币种间间隔3秒（付费通道不限流，短间隔即可）

        except Exception as e:
            log(f"推送 {coin_name} 预警失败: {e}")


# ====== 辅助函数（量化决策 + 交易执行）======

def get_dry_run_status() -> bool:
    """返回 gateio_trade 的 DRY_RUN 状态"""
    try:
        from clients.gateio_trade import DRY_RUN
        return DRY_RUN
    except ImportError:
        return True  # 模块不存在时保守为 True


def _fetch_coin_news(coin_name, news_cache, cache_file, today):
    """按币种+按天搜索新闻（与原来逻辑一致）"""
    import requests as _req
    import json
    cache_key = f"news_{coin_name}_{today}"
    now = time.time()

    # 缓存命中直接返回
    cached = news_cache.get(cache_key, {}).get("text", "")
    if cached:
        return cached.replace("**", "")

    # 未命中则搜索
    try:
        from wechat_config import AI_API_KEY
        APP_CODE = os.environ.get("AIHUBMIX_APP_CODE", "")
        news_prompt = (
            f"现在是2026年6月。请搜索{coin_name}今天的最新新闻，"
            f"只列出该币种自身相关的具体事件（含来源），不要混入其他币种或大盘行情。"
            f"控制在400字以内，必须搜索实时新闻。"
        )
        headers = {
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
        }
        if APP_CODE:
            headers["APP-Code"] = APP_CODE
        resp = _req.post(
            "https://api.aihubmix.com/v1/chat/completions",
            headers=headers,
            json={
                "model": "gpt-4o-mini-search-preview",
                "messages": [{"role": "user", "content": news_prompt}],
            },
            timeout=25,
        )
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].replace("**", "")
            news_cache[cache_key] = {"text": text, "time": now}
            with open(cache_file, "w") as f:
                json.dump(news_cache, f)
            log(f"已搜索并缓存{coin_name}的新闻")
            return text
        else:
            log(f"新闻搜索失败({coin_name}): {resp.status_code}")
    except Exception as e:
        log(f"新闻搜索异常({coin_name}): {e}")
    return ""


def _execute_trade(coin_name, symbol, action):
    """根据 AI 决策执行下单（受 DRY_RUN 保护），返回 execute_order 的结果字典"""
    if action == "HOLD":
        log(f"[交易] {coin_name} 决策为 HOLD，跳过下单")
        return {"action": "HOLD", "pnl_pct": 0.0, "pnl_usdt": 0.0}
    from clients.gateio_trade import execute_order
    result = execute_order(symbol, action, amount_usdt=10, coin_name=coin_name)
    log(f"[交易] {coin_name} -> {action}: {result['detail'][:80]}")
    return result


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
            time.sleep(1)

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
