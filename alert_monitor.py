#!/usr/bin/env python3
"""
实时异动监控模块
价格波动、RSI超卖超买、布林带突破自动推送
"""
import os, sys, time, json
import traceback
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(__file__))

# Diagnostics must exit before importing configuration or creating state/log files.
if __name__ == "__main__" and "--once" in sys.argv:
    from offline_diagnostic import main as _offline_main
    raise SystemExit(_offline_main())

# ====== 配置 ======
from wechat_config import MAJOR_COINS, ALERT_COOLDOWN, MONITOR_INTERVAL, CLIENT_NAME

# ====== 结构化决策日志（P0-5，2026-10-22）======
# 只做观测：decision_log 内部吞掉所有异常，写盘失败不影响任何决策或返回值。
try:
    from clients import decision_log as dlog
except Exception:
    try:
        import decision_log as dlog
    except Exception:
        dlog = None


def _ev(kind, **fields):
    """安全写事件（永不抛异常）。"""
    try:
        if dlog is not None:
            dlog.event(kind, **fields)
    except Exception:
        pass


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

# ====== 单实例锁（P0-1，2026-10-22）======
# 目的：阻止 `--once` 与常驻服务、或两个服务实例并行共用 alert_state/cooldown 文件。
# 约定：拿不到锁或锁机制异常均拒绝启动。离线 --once 在配置导入前退出，不共用状态。
LOCK_FILE = os.path.expanduser(f"~/.hermes/crypto_alert_{CLIENT_NAME}.lock")
_LOCK_FH = None
LOCK_ERROR = ""


def acquire_single_instance_lock():
    """P0-A：fail-closed 单实例锁。
    只有"确实拿到 flock"才返回 True；fcntl 不可用 / 锁文件打不开 / 加锁异常
    一律返回 False（调用方必须拒绝启动），不再"记个警告就放行"。
    锁文件用 a+ 非截断方式打开，避免抢锁失败时把持锁进程写入的 PID 清空。
    """
    global _LOCK_FH, LOCK_ERROR
    LOCK_ERROR = ""
    try:
        import fcntl
    except Exception as e:
        LOCK_ERROR = f"fcntl 不可用: {type(e).__name__}: {e}"
        return False
    try:
        fh = open(LOCK_FILE, "a+", encoding="utf-8")   # 非截断
    except Exception as e:
        LOCK_ERROR = f"锁文件无法打开: {type(e).__name__}: {e}"
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOCK_ERROR = "已有实例持锁"
        try:
            fh.close()
        except Exception:
            pass
        return False
    except Exception as e:
        LOCK_ERROR = f"加锁异常: {type(e).__name__}: {e}"
        try:
            fh.close()
        except Exception:
            pass
        return False
    # 只有确实拿到锁之后，才更新 PID（此前不清空既有内容）
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except Exception as e:
        log(f"⚠️ 锁已取得，但写入 PID 失败: {e}")
    _LOCK_FH = fh          # 持有到进程结束；进程退出时内核自动释放
    return True


def is_on_cooldown(coin, alert_type, cooldown, scan_id=None):
    key = f"{coin}:{alert_type}"
    now = time.time()
    if key in cooldown and now - cooldown[key] < ALERT_COOLDOWN:
        # P0-5：被冷却压制的信号此前完全不可见，这里只记日志，不改变行为
        _ev("cooldown_suppressed", scan_id=scan_id, coin=coin, rule=alert_type,
            remaining_s=int(ALERT_COOLDOWN - (now - cooldown[key])))
        return True
    cooldown[key] = now
    return False

def check_alerts(scan_id=None):
    """执行一轮巡检，返回 **二元组 (alerts, scan_id)**。

    兼容性决策（2026-10-22 第四轮，明确化）：
      · 本函数返回值由 list 变更为 (list, str)，属于**有意的不兼容变更**，
        目的是彻底移除模块级 _CURRENT_SCAN_ID 全局、消除并发串号风险；
      · 仓库内调用点已全部改为元组解包（alert_monitor.py 的 monitor_loop 与 --once）；
      · 若外部集成方仍需要旧的"只返回 list"行为，请改用兼容垫片
        check_alerts_legacy()，不要直接依赖本函数的一次返回。
      · scan_id 由调用方显式传入；不传则内部生成并随返回值带出。
    """
    from crypto_monitor import WATCHLIST, analyze_symbol

    scan_id = scan_id or (dlog.new_id("s") if dlog else "s-local")
    _snapshots = []
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
            _n0 = len(alerts)

            # 1. 价格波动预警 (相对上次检查)
            if prev and "price" in prev and prev["price"] > 0:
                pct = abs(price - prev["price"]) / prev["price"] * 100
                if pct >= vol_threshold:
                    direction = "上涨" if price > prev["price"] else "下跌"
                    if not is_on_cooldown(coin_name, "price_spike", cooldown, scan_id=scan_id):
                        alerts.append(
                            f"🚨 {coin_name} 价格异动\n"
                            f"当前: ${price:,.2f}\n"
                            f"24h: {change:+.2f}%\n"
                            f"较上次: {direction} {pct:.1f}%\n"
                            f"RSI: {rsi:.1f}\n"
                            f"⏰ {now_str}"
                        )

            # 2. RSI超卖/超买
            if rsi <= 25 and not is_on_cooldown(coin_name, "rsi_oversold", cooldown, scan_id=scan_id):
                alerts.append(
                    f"⚠️ {coin_name} RSI超卖\n"
                    f"价: ${price:,.2f}  RSI: {rsi:.1f}\n"
                    f"24h: {change:+.2f}%\n"
                    f"短期可能反弹，注意风险\n"
                    f"⏰ {now_str}"
                )
            elif rsi >= 75 and not is_on_cooldown(coin_name, "rsi_overbought", cooldown, scan_id=scan_id):
                alerts.append(
                    f"⚠️ {coin_name} RSI超买\n"
                    f"价: ${price:,.2f}  RSI: {rsi:.1f}\n"
                    f"24h: {change:+.2f}%\n"
                    f"注意回调风险\n"
                    f"⏰ {now_str}"
                )

            # 3. 布林带突破
            if bb_l > 0 and bb_u > 0:
                if price <= bb_l and not is_on_cooldown(coin_name, "bb_lower", cooldown, scan_id=scan_id):
                    alerts.append(
                        f"📉 {coin_name} 跌破布林下轨\n"
                        f"价: ${price:,.2f}  下轨: ${bb_l:,.2f}\n"
                        f"RSI: {rsi:.1f}  24h: {change:+.2f}%\n"
                        f"⏰ {now_str}"
                    )
                elif price >= bb_u and not is_on_cooldown(coin_name, "bb_upper", cooldown, scan_id=scan_id):
                    alerts.append(
                        f"📈 {coin_name} 涨破布林上轨\n"
                        f"价: ${price:,.2f}  上轨: ${bb_u:,.2f}\n"
                        f"RSI: {rsi:.1f}  24h: {change:+.2f}%\n"
                        f"⏰ {now_str}"
                    )

            state[coin_name] = {"price": price, "time": time.time()}
            try:
                _snapshots.append({
                    "coin": coin_name, "price": price, "rsi": round(float(rsi), 1),
                    "bb": [round(float(bb_l), 2), round(float(bb_u), 2)],
                    "chg_24h": round(float(change), 2),
                    "alerts_fired": len(alerts) - _n0,
                })
            except Exception:
                pass

        except Exception as e:
            log(f"检查 {coin_name} 出错: {e}")
            continue

    save_json(STATE_FILE, state)
    save_json(COOLDOWN_FILE, cooldown)
    _ev("scan", scan_id=scan_id, coins=_snapshots, alerts=len(alerts),
        interval_s=MONITOR_INTERVAL)
    return alerts, scan_id

def check_alerts_legacy(scan_id=None):
    """兼容垫片：只返回 alerts（丢弃 scan_id），供仍按旧签名集成的外部调用方使用。
    内部代码不要用它（内部一律用 check_alerts() 的二元组，保证 scan_id 不丢）。"""
    alerts, _sid = check_alerts(scan_id=scan_id)
    return alerts


# ══════════════════════════════════════════════════════════════
# 预警推送总开关（2026-10-11 用户要求：暂停「📈预警 + 📊简析 + 💤交易决策」这条推送）
# 代码完整保留：改回 True 即恢复原有行为（含该路径的自动下单）。
# 注意：7 角色深度报告的"雷达触发 + 执行"不受这个开关影响（用户要求保留）。
# ══════════════════════════════════════════════════════════════
COIN_ALERT_PUSH_ENABLED = False


def push_alerts(alerts, scan_id=None):
    if not alerts:
        return
    # ══════════════════════════════════════════════════════════
    # yunwu.ai 连通性探测：不可用时跳过本轮全部推送，
    # 避免向用户推送一堆 "AI 调用失败" 的无效消息。
    # （任何 HTTP 响应都算网络可达；只有超时/断连才算不可用）
    # ══════════════════════════════════════════════════════════
    if not _is_yunwu_ready():
        log(f"⛔ yunwu.ai 不可达，跳过本轮 {len(alerts)} 条预警推送（AI 不可用）")
        _notify_ai_down_once(len(alerts))   # 只在"正常→失败"那一次提醒用户
        return

    from wechat_push import send_simple_message
    from crypto_monitor import build_prompt, ask_ai, WATCHLIST
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
                _ev("shallow_decision", scan_id=scan_id, coin=coin_name, action=action,
                    reason=str(reason)[:200], price=data.get("price"),
                    rsi=data.get("rsi"), bb_lower=data.get("bb_lower"),
                    bb_upper=data.get("bb_upper"), chg_24h=data.get("change_24h"),
                    vol_ratio=data.get("vol_ratio"),
                    news_status=("ok" if news_text else "empty_or_failed"))

                # ── 失败分类（2026-10-15 修正）──
                # A) 模型通道全挂（"所有模型调用失败"）→ 这才是"AI 不可用"，提醒一次并跳过
                # B) 只是输出解析不出来（通道是通的）→ 不算通道故障，只记日志，本次按 HOLD 处理
                if "所有模型调用失败" in reason:
                    log(f"⚠️ {coin_name} AI 通道调用失败: {reason}")
                    _notify_ai_down_once(len(alerts), detail=reason)
                    continue
                if "解析失败" in reason:
                    log(f"⚠️ {coin_name} 浅层决策输出无法解析（模型可用，本次按 HOLD 处理）: {reason}")
                _ai_health_reset()   # AI 通道正常 → 复位失败提醒状态（不发消息）

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
                    _ev("committee_wake", scan_id=scan_id, coin=coin_name, action=action,
                        reason=str(reason)[:200])

                    try:
                        import seven_roles_committee
                        _ok = seven_roles_committee.run_committee(
                            coin_name, symbol, reason, scan_id=scan_id)
                        if _ok is False:
                            # 委员会中途因 AI 调用失败中止 → 只提醒一次（不推假报告）
                            log(f"❌ {coin_name} 7角色投研因 AI 调用失败中止")
                            _notify_ai_down_once(len(alerts), detail="7 角色投研中途失败（AI 调用异常），本次未给出交易结论，也未下单")
                        else:
                            _ai_health_reset()
                    except Exception as e:
                        err_msg = f"{chr(0x26a0)}{chr(0xfe0f)} 7角色深度投研唤醒失败: {e}"
                        log(err_msg)
                        send_simple_message(err_msg)

                    # 7角色接管，跳过下方原有的下单和推送逻辑
                    continue
                # =======================================================

                # ── 预警推送总开关（2026-10-11 用户暂停）──
                # 只跳过"简析 + 下单 + 推送"这一段：不调 AI（省 token）、不发消息、不下单。
                # 雷达触发/7角色深度投研在上方，照常工作。
                if not COIN_ALERT_PUSH_ENABLED:
                    log(f"[预警推送已暂停] {coin_name} 浅层决策={action}，跳过简析/下单/推送")
                    continue

                # 交易信号推送给用户
                action_emoji = {"BUY": chr(0x1f7e2) + " 买入信号", "SELL": chr(0x1f534) + " 卖出信号", "HOLD": chr(0x26aa) + " 持有观望"}
                dr = get_dry_run_status()
                dry_note = f"\n{chr(0x26a0)}{chr(0xfe0f)} DRY RUN 模式，不会真实下单" if dr else ""

                # ── 执行下单（DRY_RUN 保护），捕获返回值用于 PnL ──
                trade_result = _execute_trade(coin_name, symbol, action)

                # 下单结果提示：失败必须如实带原因，不能只报"买入信号"（2026-10-07 教训）
                trade_note = ""
                if action in ("BUY", "SELL") and trade_result:
                    if not trade_result.get("success"):
                        _detail = str(trade_result.get("detail") or "下单失败，详见服务器日志").strip()
                        trade_note = f"\n{chr(0x26a0)}{chr(0xfe0f)} 下单未成功: {_detail[:120]}"

                # 提取 PnL（仅 SELL + 有盈亏时）
                pnl_str = ""
                if action == "SELL" and trade_result:
                    pnl_pct = trade_result.get("pnl_pct", 0)
                    pnl_usdt = trade_result.get("pnl_usdt", 0)
                    if pnl_pct != 0:
                        sign = "+" if pnl_pct >= 0 else ""
                        direction = "赚" if pnl_pct >= 0 else "亏"
                        _label = "模拟平仓收益" if dr else "平仓盈亏"
                        pnl_str = f"\n{chr(0x1f9f8)} {_label}: {sign}{pnl_pct}% (大约{direction} {abs(pnl_usdt):.2f} USDT)"

                decision_text = (
                    f"---\n"
                    f"{chr(0x1f4a4)} {coin_name} 交易决策\n"
                    f"{action_emoji.get(action, action)}\n"
                    f"理由: {reason}{dry_note}{trade_note}{pnl_str}"
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
                if not ai_text or ai_text.strip() == "":
                    ai_text = f"{coin_name} 当前价 ${data.get('price',0):,.2f}，RSI {data.get('rsi',50):.1f}，24h {data.get('change_24h',0):+.2f}%，暂无明显方向"

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


_YUNWU_LAST_ERROR = ""   # 最近一次探测失败的原因（供"首次失败提醒"使用）


def _is_yunwu_ready(timeout: float = 20.0) -> bool:
    """
    探测 openlux 是否可用（AI 主通道）。

    作用：这是一道"成本/时间保护闸"，只在**真正不可用**时才拦。
    - 为什么要探测：如果完全不检查，AI 挂掉时每轮都会硬跑 3 模型 × 3 次重试 × 60 秒超时
      （最坏 9 分钟），把 5 分钟一次的监控循环堵死，还会推出一堆"AI 调用失败"的垃圾消息。
    - 2026-10-12 调整：超时从 5 秒放宽到 20 秒，并在超时后重试 1 次。
      原因：实测正常请求延迟在 1.8~5.5 秒之间波动（5 秒门槛会误报，10/12 01:51 就是这么误报的）。
    - 真正的故障（模型下线 404、密钥失效、网络不可达）都是秒级报错，不受这个超时影响。

    失败原因记入模块级 _YUNWU_LAST_ERROR，供"首次失败提醒"使用。
    """
    global _YUNWU_LAST_ERROR
    import requests as _req
    last_err = ""
    for attempt in (1, 2):
        try:
            from wechat_config import YUNWU_API_KEY
            resp = _req.post(
                "https://api.openlux.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {YUNWU_API_KEY}"},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                    "temperature": 0,
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                _YUNWU_LAST_ERROR = ""
                return True
            # 非 200：把服务商的报错原文带回来（例如 model_not_found / 无可用渠道）
            try:
                _err = resp.json().get("error", {}).get("message", "")
            except Exception:
                _err = (resp.text or "")[:120]
            last_err = f"HTTP {resp.status_code} {_err}".strip()
            break               # 明确的 HTTP 错误 → 不重试，直接判定
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:100]}"
            log(f"[openlux探测] 第{attempt}次失败: {str(e)[:70]}")
            if attempt == 1:
                time.sleep(2)   # 抖动：等 2 秒再试一次（避免一次网络抖动就误判）
                continue
    _YUNWU_LAST_ERROR = last_err
    return False



# ══════════════════════════════════════════════════════════════
# AI 不可用提醒状态机（用户 2026-10-11 指定）：
#   正常→失败：提醒 1 次；连续失败：保持安静；恢复成功后自动复位，
#   之后若再失败，仍只提醒那一次。
# ══════════════════════════════════════════════════════════════
AI_HEALTH_FILE = os.path.expanduser("~/.hermes/ai_health_state.json")


def _ai_health_load() -> dict:
    try:
        with open(AI_HEALTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _ai_health_save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(AI_HEALTH_FILE), exist_ok=True)
        tmp = AI_HEALTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, AI_HEALTH_FILE)
    except Exception as e:
        log(f"AI 健康状态写入失败: {e}")


def _notify_ai_down_once(n_alerts: int = 0, detail: str = "") -> None:
    """AI 首次调用失败时提醒一次；连续失败不再重复（恢复后状态由 _ai_health_reset 复位）。"""
    st = _ai_health_load()
    if st.get("ai_failing"):
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    st["ai_failing"] = True
    st["first_fail_at"] = ts
    _ai_health_save(st)
    try:
        from wechat_push import send_simple_message
        reason = detail or _YUNWU_LAST_ERROR or "未知原因"
        send_simple_message(
            f"⚠️ AI 分析通道调用失败\n"
            f"🕐 时间: {ts}\n"
            f"❗ 原因: {reason}\n"
            f"📉 影响: 这期间的行情分析和 7 角色投研无法进行（价格监测照常跑，但没有 AI 判断）\n"
            f"（连续失败不再重复提醒；AI 恢复后若再失败会再提醒一次）"
        )
        log(f"⚠️ 已推送 AI 首次失败提醒: {reason}")
    except Exception as e:
        log(f"AI 失败提醒推送失败: {e}")


def _ai_health_reset() -> None:
    """AI 恢复可用时复位状态（不发消息），使下一次失败能再提醒一次。"""
    st = _ai_health_load()
    if st.get("ai_failing"):
        st["ai_failing"] = False
        st["recovered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _ai_health_save(st)
        log("✅ AI 已恢复，失败提醒状态已复位")


def _fetch_coin_news(coin_name, news_cache, cache_file, today):
    """从 CoinTelegraph RSS 获取真实新闻 + 免费模型提炼"""
    import requests as _req
    import json
    import re as _re
    import xml.etree.ElementTree as ET
    cache_key = f"news_{coin_name}_{today}"
    now = time.time()

    # 缓存命中直接返回
    cached = news_cache.get(cache_key, {}).get("text", "")
    if cached:
        return cached.replace("**", "")

    # 从 CoinTelegraph RSS 抓取新闻
    raw_news = []
    try:
        urls = [
            "https://cointelegraph.com/rss",
        ]
        rss_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        for url in urls:
            try:
                r = _req.get(url, headers=rss_headers, timeout=10)
                if r.status_code == 200:
                    root = ET.fromstring(r.content)
                    items = root.findall('./channel/item')
                    coin_upper = coin_name.upper()
                    matched = []
                    for item in items:
                        title = item.find('title').text if item.find('title') is not None else ""
                        desc = item.find('description').text if item.find('description') is not None else ""
                        raw = f"{title} {desc}"
                        if coin_upper in raw.upper():
                            matched.append(raw)
                    top3 = matched[:3] if matched else []
                    for news in top3:
                        clean = _re.sub(r'<[^>]+>', '', news).strip()[:500]
                        raw_news.append(clean)
                    if top3:
                        break
            except:
                continue

        news_text = "\n---\n".join(raw_news) if raw_news else ""

        # 用免费模型提炼（仅当有原始新闻时）
        if news_text:
            from wechat_config import AI_API_KEY
            APP_CODE = os.environ.get("AIHUBMIX_APP_CODE", "")
            prompt = (
                "You are a crypto analyst. Here are the latest real news just fetched:\n"
                + news_text + "\n"
                + "Based on these real news, briefly summarize market sentiment for "
                + coin_name + " in Chinese, within 150 chars."
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
                    "model": "gpt-4.1-nano-free",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"].replace("**", "")
                news_cache[cache_key] = {"text": text, "time": now}
                with open(cache_file, "w") as f:
                    json.dump(news_cache, f)
                log(f"已获取真实新闻并缓存{coin_name}的市场点评")
                return text

        # AI 调用失败或没有新闻时，返回原始新闻
        if raw_news:
            fallback = "\U0001f4f0 Latest News:\n" + "\n".join(raw_news[:2])
            news_cache[cache_key] = {"text": fallback, "time": now}
            with open(cache_file, "w") as f:
                json.dump(news_cache, f)
            return fallback
            
    except Exception as e:
        log(f"获取新闻异常({coin_name}): {e}")

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


def start_protection_worker(stop):
    import threading
    def work():
        from clients.gateio_trade import run_protection_cycle
        while not stop.is_set():
            try:
                outcome = run_protection_cycle()
                if outcome.get("exits") or outcome.get("blocked_new_risk"):
                    _ev("paper_protection", result=outcome)
            except Exception as exc:
                _ev("paper_protection_error", error_type=type(exc).__name__)
            stop.wait(10)
    worker = threading.Thread(target=work, name="paper-protection", daemon=True)
    worker.start()
    return worker


def monitor_loop():
    import threading
    stop = threading.Event()
    worker = start_protection_worker(stop)
    log("实时异动监控已启动；纸面风险保护独立运行")
    try:
        while True:
            try:
                alerts, scan_id = check_alerts()
                if alerts:
                    push_alerts(alerts, scan_id=scan_id)
                time.sleep(MONITOR_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log("巡检失败: " + type(exc).__name__)
                time.sleep(1)
    finally:
        stop.set()
        worker.join(timeout=1)


if __name__ == "__main__":
    import sys

    # P0-1：单实例锁（常驻服务与 --once 都要先拿到锁）
    if not acquire_single_instance_lock():
        log(f"⛔ 单实例保护不可用，拒绝启动（{LOCK_ERROR}；锁文件 {LOCK_FILE}）")
        sys.exit(1)

    monitor_loop()
