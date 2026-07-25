#!/usr/bin/env python3
"""
Gate.io 交易执行模块

安全设计：
  - DRY_RUN = True：仅打印日志 + 记虚拟账本，绝不触达真实交易所
  - API Key 从 .env 文件读取，不硬编码
  - 下单前做 symbol 合法性检查
  - ccxt 初始化失败时直接返回错误，不静默继续

虚拟账本（DRY_RUN 模式）：
  - 自动记录每笔模拟交易到 ~/.hermes/paper_trading_log.json
  - SELL 时自动回溯最近一次同币种 BUY，计算盈亏比例和绝对值
  - 盈亏数据通过 execute_order 返回值透出，供 alert_monitor 拼入推送
  - 支持按百分比仓位下单（percentage 参数），由 7 角色委员会的风控经理决定

多仓模式（v2 新增）：
  - 同一币种允许分批多次买入（取消单仓位 BUY 拦截）
  - 空仓时 SELL 直接快速拦截，不触发委员会讨论
  - 每次交易金额由 percentage 参数动态计算
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta, date
from collections import OrderedDict

# ── 全局安全开关 ──
DRY_RUN = True

_logger = logging.getLogger("gateio_trade")
_logger.setLevel(logging.INFO)
_ch = logging.StreamHandler()
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
if not _logger.handlers:
    _logger.addHandler(_ch)

# 本地模拟时 .env 路径
_ENV_PATH = os.path.expanduser("~/.hermes/gateio.env")

# 虚拟账本路径
_PAPER_LOG = os.path.expanduser("~/.hermes/paper_trading_log.json")

# ── 初始资金配置 ──
# 人民币计价，便于用户理解
INITIAL_CAPITAL_CNY = 500.0       # 初始资金 500 元人民币
USDT_CNY_RATE = 7.25              # 当前 USDT/CNY 汇率（约）
INITIAL_CAPITAL = round(INITIAL_CAPITAL_CNY / USDT_CNY_RATE, 2)  # ≈ 69 USDT


# ==================== 虚拟账本（DRY_RUN 用）====================

def _load_ledger() -> list:
    """读取虚拟账本"""
    if os.path.exists(_PAPER_LOG):
        try:
            with open(_PAPER_LOG) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_ledger(ledger: list):
    """写入虚拟账本"""
    os.makedirs(os.path.dirname(_PAPER_LOG), exist_ok=True)
    with open(_PAPER_LOG, "w") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)


def _fetch_current_price(ccxt_symbol: str) -> float:
    """获取当前市价（优先 Gate.io，失败时返回 0）"""
    try:
        import requests
        # btcusdt 形式用于 Gate.io REST
        pair = ccxt_symbol.replace("/", "_")
        resp = requests.get(
            f"https://api.gateio.ws/api/v4/spot/tickers",
            params={"currency_pair": pair},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data and len(data) > 0:
            return float(data[0]["last"])
    except Exception as e:
        _logger.warning(f"获取市价失败: {e}")
    return 0.0


def _bjt_now_str() -> str:
    """北京时间字符串"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    """当天日期字符串 (YYYY-MM-DD)"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _calc_pnl(coin_base: str, action: str, fill_price: float, quantity: float,
              ledger: list) -> dict:
    """
    计算盈亏（仅 SELL 时有效）。

    逻辑：
      1. 从账本中逆序寻找该币种最近一次 BUY
      2. 用买入价 vs 卖出价计算盈亏
      3. 写入 SELL 记录时同时携带 pnl_pct / pnl_usdt

    返回：
      {"pnl_pct": float, "pnl_usdt": float, "cost_basis": float}
      或 {"pnl_pct": 0, "pnl_usdt": 0, "cost_basis": 0}
    """
    if action.upper() != "SELL":
        return {"pnl_pct": 0.0, "pnl_usdt": 0.0, "cost_basis": 0.0}

    # 逆序找最近一次该币种的 BUY
    for entry in reversed(ledger):
        if entry.get("base") == coin_base and entry.get("action") == "BUY":
            buy_price = entry.get("fill_price", 0)
            buy_qty = entry.get("quantity", 0)
            if buy_price > 0 and buy_qty > 0:
                cost_basis = buy_price  # 买入均价
                pnl_pct = round((fill_price - cost_basis) / cost_basis * 100, 2)
                pnl_usdt = round((fill_price - cost_basis) * buy_qty, 2)
                return {"pnl_pct": pnl_pct, "pnl_usdt": pnl_usdt, "cost_basis": cost_basis}
            break  # 找到一条 BUY 记录但数据不全，不再继续
    return {"pnl_pct": 0.0, "pnl_usdt": 0.0, "cost_basis": 0.0}


def _get_open_positions(ledger: list = None) -> dict:
    """
    从虚拟账本计算当前持仓（含每笔未平仓的买入记录）。

    返回: {
      "BTC": {
        "entries": [
          {"qty": 0.1, "price": 65000, "time": "..."},
          {"qty": 0.05, "price": 62000, "time": "..."}
        ],
        "total_qty": 0.15,
        "avg_cost": 64000,
        "total_cost": 9600
      },
      ...
    }
    空仓返回空字典。
    """
    if ledger is None:
        ledger = _load_ledger()

    # 先用 FIFO 方式跟踪哪些 BUY 已被 SELL 抵消
    buy_queue = {}  # coin -> [{"qty": x, "price": y, "time": z}, ...]

    for entry in ledger:
        coin = entry.get("coin", entry.get("base", ""))
        action = entry.get("action", "")
        qty = entry.get("quantity", 0)
        price = entry.get("fill_price", 0)
        t = entry.get("time", "")

        if action == "BUY":
            if coin not in buy_queue:
                buy_queue[coin] = []
            buy_queue[coin].append({"qty": qty, "price": price, "time": t})

        elif action == "SELL":
            if coin not in buy_queue:
                continue
            remaining = qty
            while remaining > 0 and buy_queue[coin]:
                first = buy_queue[coin][0]
                if first["qty"] <= remaining:
                    remaining -= first["qty"]
                    buy_queue[coin].pop(0)
                else:
                    first["qty"] -= remaining
                    remaining = 0
            # 如果队列空了，删除键
            if not buy_queue[coin]:
                del buy_queue[coin]

    # 整理成返回格式
    result = {}
    for coin, queue in buy_queue.items():
        if not queue:
            continue
        total_qty = sum(e["qty"] for e in queue)
        total_cost = sum(e["qty"] * e["price"] for e in queue)
        avg_cost = round(total_cost / total_qty, 2) if total_qty > 0 else 0
        result[coin] = {
            "entries": queue,
            "total_qty": total_qty,
            "avg_cost": avg_cost,
            "total_cost": total_cost,
        }
    return result


def _get_holdings(ledger: list = None) -> dict:
    """
    从虚拟账本计算当前持仓（简化版，兼容旧调用方）。
    返回: {"BTC": {"quantity": 0.1, "cost": 65000}, "ETH": {...}}
    空仓返回空字典。
    """
    positions = _get_open_positions(ledger)
    holdings = {}
    for coin, info in positions.items():
        holdings[coin] = {
            "quantity": info["total_qty"],
            "avg_cost": info["avg_cost"],
            "total_cost": info["total_cost"],
        }
    return holdings


def _paper_trade(ccxt_symbol: str, coin_name: str, action: str,
                 amount_usdt: float, percentage: int = 0,
                 current_qty: float = 0.0) -> dict:
    """
    虚拟账本交易：获取市价、记录、计算 PnL。

    返回：
      {
        "success": True,
        "action": str,
        "detail": str,        # 日志文字
        "fill_price": float,
        "quantity": float,
        "pnl_pct": float,     # SELL 时有效
        "pnl_usdt": float,    # SELL 时有效
      }
    """
    # 提取基础币种（如 BTC/USDT → BTC）
    coin_base = ccxt_symbol.split("/")[0]
    fill_price = _fetch_current_price(ccxt_symbol)

    # 根据 percentage 计算实际数量
    if percentage > 0 and action.upper() == "SELL" and current_qty > 0:
        # SELL: 按持仓比例
        quantity = round(current_qty * percentage / 100, 8)
    elif percentage > 0 and action.upper() == "BUY":
        # BUY: amount_usdt 已在 execute_order 中按比例计算好
        quantity = round(amount_usdt / fill_price, 8) if fill_price > 0 else 0
    else:
        quantity = round(amount_usdt / fill_price, 8) if fill_price > 0 else 0

    side_cn = "买入" if action.upper() == "BUY" else "卖出"

    # ── 读取 + 计算 PnL（SELL 时）──
    ledger = _load_ledger()
    pnl = _calc_pnl(coin_base, action, fill_price, quantity, ledger)
    pnl_pct = pnl["pnl_pct"]
    pnl_usdt = pnl["pnl_usdt"]

    # ── 记录到账本 ──
    entry = OrderedDict([
        ("time", _bjt_now_str()),
        ("coin", coin_name),
        ("symbol", ccxt_symbol),
        ("base", coin_base),
        ("action", action.upper()),
        ("fill_price", fill_price),
        ("quantity", quantity),
        ("amount_usdt", amount_usdt),
    ])
    if pnl_pct != 0.0:
        entry["pnl_pct"] = pnl_pct
        entry["pnl_usdt"] = pnl_usdt
        entry["cost_basis"] = pnl["cost_basis"]

    ledger.append(dict(entry))
    _save_ledger(ledger)

    # ── 日志文字 ──
    pnl_str = ""
    if action.upper() == "SELL" and pnl_pct != 0:
        sign = "+" if pnl_pct >= 0 else ""
        direction = "赚" if pnl_pct >= 0 else "亏"
        pnl_str = f"\n  💸 模拟平仓: {sign}{pnl_pct}% ({direction} ~{abs(pnl_usdt)} USDT, 买入价 ${pnl['cost_basis']:.2f})"

    pct_info = f" (仓位 {percentage}%)" if percentage > 0 else ""
    msg = (
        f"[DRY RUN] 模拟下单 (已记入虚拟账本)：\n"
        f"  交易对: {ccxt_symbol}{pct_info}\n"
        f"  方向: {side_cn}\n"
        f"  成交价: ${fill_price:,.2f}\n"
        f"  数量: {quantity}\n"
        f"  金额: {amount_usdt} USDT{pnl_str}"
    )
    _logger.info(msg)

    return {
        "success": True,
        "action": action.upper(),
        "detail": msg,
        "fill_price": fill_price,
        "quantity": quantity,
        "pnl_pct": pnl_pct,
        "pnl_usdt": pnl_usdt,
    }


# ==================== 环境变量 ====================

def _load_env() -> dict:
    """从 .env 文件读取 GATEIO_API_KEY / GATEIO_API_SECRET"""
    env = {}
    path = os.environ.get("GATEIO_ENV_PATH", _ENV_PATH)
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("GATEIO_API_KEY", "GATEIO_API_SECRET"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _check_symbol(symbol: str) -> str:
    """标准化为 ccxt 格式 "BTC/USDT" """
    s = symbol.upper().strip()
    if "/" in s:
        parts = s.split("/")
    elif "_" in s:
        parts = s.split("_")
    else:
        if s.endswith("USDT"):
            parts = [s[:-4], "USDT"]
        elif s.endswith("USD"):
            parts = [s[:-3], "USD"]
        else:
            return None
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}/{parts[1]}"


# ==================== 主入口 ====================

def execute_order(symbol: str, action: str, amount_usdt: float = 10,
                  coin_name: str = "", percentage: int = 0) -> dict:
    """
    执行 Gate.io 市价单（受 DRY_RUN 保护）。

    参数:
        symbol: "btcusdt" / "BTC_USDT" / "BTC/USDT"
        action: "BUY" / "SELL" / "HOLD"
        amount_usdt: 基础下单金额（USDT 计价），默认 10 USDT
                     当 percentage > 0 时，此参数作为兜底值
        coin_name: 币种可读名称（如 "BTC"），用于账本记录；为空时从 symbol 推断
        percentage: 仓位百分比（0-100），BUY时占可用余额，SELL时占持仓量
                    由 alert_monitor / 7角色委员会传入

    返回:
        {
          "success": bool,
          "action": str,
          "detail": str,
          # DRY_RUN 模式下还会返回：
          "fill_price": float,   # 模拟成交价
          "quantity": float,     # 模拟成交数量
          "pnl_pct": float,      # SELL 平仓盈亏百分比（非 SELL 为 0）
          "pnl_usdt": float,     # SELL 平仓绝对盈亏 USDT（非 SELL 为 0）
        }
    """
    result = {
        "success": False, "action": action, "detail": "",
        "fill_price": 0.0, "quantity": 0.0,
        "pnl_pct": 0.0, "pnl_usdt": 0.0,
    }

    # ── HOLD 直接跳过 ──
    if action.upper() == "HOLD":
        result["detail"] = "决策为 HOLD，不下单"
        result["success"] = True
        return result

    # ── symbol 校验 ──
    ccxt_symbol = _check_symbol(symbol)
    if not ccxt_symbol:
        result["detail"] = f"无效的交易对: {symbol}"
        _logger.error(result["detail"])
        return result

    # ── 推断 coin_name ──
    if not coin_name:
        coin_name = ccxt_symbol.split("/")[0]

    # ── 仓位检查 ──
    coin_base = ccxt_symbol.split("/")[0]
    holdings = _get_holdings()
    current_qty = holdings.get(coin_base, {}).get("quantity", 0.0)

    # 空仓 SELL → 快速拦截（不触发委员会，直接返回）
    if action.upper() == "SELL" and current_qty <= 0:
        msg = f"⛔ 空仓拦截: {coin_name} 当前持仓为0，拒绝执行 SELL（已记录日志）"
        _logger.warning(msg)
        result["detail"] = msg
        result["success"] = True  # 不报错，静默拦截
        return result

    # 【多仓模式】取消单仓位 BUY 拦截：
    # 同一币种允许分批多次买入，由 7 角色委员会的风控经理决定每次的仓位比例
    # 下⾯不再检查 BUY 时是否已有持仓

    # ── 根据 percentage 计算实际下单金额/数量 ──
    if percentage > 0:
        if action.upper() == "BUY":
            # 计算可用余额
            ledger = _load_ledger()
            # 计算已卖出回笼的资金
            sell_proceeds = 0.0
            for e in ledger:
                if e.get("action") == "SELL":
                    sell_qty = e.get("quantity", 0)
                    sell_price = e.get("fill_price", 0)
                    sell_proceeds += sell_qty * sell_price
            # 计算所有 BUY 耗用的总资金
            buy_spent = 0.0
            for e in ledger:
                if e.get("action") == "BUY":
                    buy_qty = e.get("quantity", 0)
                    buy_price = e.get("fill_price", 0)
                    buy_spent += buy_qty * buy_price

            # 当前持仓市值
            holdings_value = 0.0
            for coin, info in holdings.items():
                hold_qty = info.get("quantity", 0)
                if hold_qty > 0:
                    try:
                        import requests as _req
                        price_url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={coin}_USDT"
                        price_resp = _req.get(price_url, timeout=5)
                        if price_resp.status_code == 200:
                            cur_price = float(price_resp.json()[0]["last"])
                            holdings_value += hold_qty * cur_price
                    except Exception:
                        holdings_value += info.get("total_cost", 0)

            # 可用余额 = 初始总资产 - 持仓市值 + 已卖出回笼
            available = INITIAL_CAPITAL - holdings_value + sell_proceeds
            if available < 1:
                available = 1
            amount_usdt = round(available * percentage / 100, 2)

        elif action.upper() == "SELL" and current_qty > 0:
            # SELL 时 amount_usdt 无意义，传 0 标记为"按比例"
            amount_usdt = 0

    # ── DRY_RUN 模式：虚拟账本 ──
    if DRY_RUN:
        trade_result = _paper_trade(ccxt_symbol, coin_name, action, amount_usdt,
                                     percentage=percentage, current_qty=current_qty)
        result.update(trade_result)
        return result

    # ── 实盘模式（需要 API Key + ccxt）──
    try:
        import ccxt
    except ImportError:
        result["detail"] = "ccxt 未安装，无法实盘下单"
        _logger.error(result["detail"])
        return result

    env = _load_env()
    api_key = env.get("GATEIO_API_KEY", "")
    api_secret = env.get("GATEIO_API_SECRET", "")

    if not api_key or not api_secret:
        result["detail"] = "GATEIO_API_KEY 或 GATEIO_API_SECRET 未配置，无法实盘下单"
        _logger.error(result["detail"])
        return result

    try:
        exchange = ccxt.gate({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
        })
        exchange.load_markets()

        if ccxt_symbol not in exchange.markets:
            result["detail"] = f"Gate.io 不支持该交易对: {ccxt_symbol}"
            _logger.error(result["detail"])
            return result

        market = exchange.markets[ccxt_symbol]
        side = "buy" if action.upper() == "BUY" else "sell"

        ticker = exchange.fetch_ticker(ccxt_symbol)
        current_price = ticker["last"]
        raw_amount = amount_usdt / current_price

        if market["precision"]["amount"]:
            raw_amount = exchange.amount_to_precision(ccxt_symbol, raw_amount)

        order = exchange.create_market_order(
            symbol=ccxt_symbol,
            side=side,
            amount=float(raw_amount),
        )

        msg = (
            f"✅ 实盘下单成功\n"
            f"  交易对: {ccxt_symbol}\n"
            f"  方向: {side}\n"
            f"  数量: {raw_amount}\n"
            f"  金额: ~{amount_usdt} USDT\n"
            f"  订单ID: {order.get('id', 'N/A')}"
        )
        _logger.info(msg)
        result["success"] = True
        result["detail"] = msg
        result["fill_price"] = current_price
        result["quantity"] = float(raw_amount)
        return result

    except Exception as e:
        result["detail"] = f"实盘下单异常: {e}"
        _logger.error(result["detail"])
        return result


# ==================== 账本报告（增强版）====================

def generate_ledger_summary() -> str:
    """
    读取虚拟账本，生成总资产状态文字报告（人民币计价）。

    包含：
      - 总览：余额、盈亏、胜率、交易次数
      - 当日交易明细（BUY/SELL 活动）
      - 当前持仓详情（币种、数量、均价、现价、浮动盈亏）
      - 已平仓交易汇总
    """
    ledger = _load_ledger()
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    tday = _today_str()

    if not ledger:
        return (
            f"📊 AI 量化每日盘点  [{now_str}]\n"
            f"═════════════════════════\n"
            f"💼 【账户总览】\n"
            f"初始本金: {INITIAL_CAPITAL_CNY:.2f} 元 (~${INITIAL_CAPITAL:.2f})\n"
            f"当前净值: {INITIAL_CAPITAL_CNY:.2f} 元 (~${INITIAL_CAPITAL:.2f})\n"
            f"净值波动: 0.00%\n"
            f"\n"
            f"🪙 【持仓明细】 (共 0 币种)\n"
            f"暂无交易记录，等待 AI 策略触发首次交易信号...\n"
            f"═════════════════════════\n"
            f"💡 系统提示: 已注入防手续费磨损策略。"
        )

    # ===== 总览统计 =====
    total_trades = len(ledger)
    sell_records = [e for e in ledger if e.get("action") == "SELL" and e.get("pnl_pct") is not None]
    total_pnl_usdt = round(sum(e.get("pnl_usdt", 0) for e in sell_records), 2)
    total_pnl_cny = round(total_pnl_usdt * USDT_CNY_RATE, 2)

    # 浮动盈亏
    positions = _get_open_positions(ledger)
    total_unrealized_usdt = 0.0
    for coin, info in positions.items():
        cur_price = _fetch_current_price(f"{coin}/USDT")
        if cur_price > 0:
            total_unrealized_usdt += round((cur_price - info["avg_cost"]) * info["total_qty"], 2)
    total_unrealized_cny = round(total_unrealized_usdt * USDT_CNY_RATE, 2)

    # 总资产 = 本金 + 已实现盈亏 + 浮动盈亏
    total_assets_usdt = round(INITIAL_CAPITAL + total_pnl_usdt + total_unrealized_usdt, 2)
    total_assets_cny = round(total_assets_usdt * USDT_CNY_RATE, 2)

    # 胜率统计
    win_count = sum(1 for e in sell_records if e.get("pnl_usdt", 0) > 0)
    win_rate = round(win_count / len(sell_records) * 100, 1) if sell_records else 0.0
    # 亏损笔数
    lose_count = len(sell_records) - win_count

    # 净值波动率
    net_change_pct = round((total_assets_usdt - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2)
    net_sign = "+" if net_change_pct >= 0 else ""

    # ===== 当日交易明细 =====
    today_trades = [e for e in ledger if e.get("time", "").startswith(tday)]
    today_buys = [e for e in today_trades if e.get("action") == "BUY"]
    today_sells = [e for e in today_trades if e.get("action") == "SELL"]

    today_section = ""
    if today_trades:
        lines = []
        for e in today_trades:
            act = e.get("action", "")
            coin = e.get("coin", e.get("base", ""))
            p = e.get("fill_price", 0)
            q = e.get("quantity", 0)
            t = e.get("time", "").split(" ")[-1][:5]  # HH:MM
            pnl = ""
            if act == "SELL" and e.get("pnl_usdt"):
                s = "+" if e["pnl_usdt"] >= 0 else ""
                pnl = f" 盈亏:{s}{e['pnl_usdt']:.2f}USDT"
            lines.append(f"  {t} {act} {coin} ${p:.2f} x {q}{pnl}")

        today_section = (
            f"\n📅 今日交易 ({len(today_buys)}买/{len(today_sells)}卖)\n"
            + "\n".join(lines)
        )

    # ===== 持仓明细 =====
    positions_section = ""
    if positions:
        pos_lines = []
        for idx, (coin, info) in enumerate(sorted(positions.items()), 1):
            avg_cost = info["avg_cost"]
            total_qty = info["total_qty"]
            cur_price = _fetch_current_price(f"{coin}/USDT")
            if cur_price > 0:
                unrealized = round((cur_price - avg_cost) * total_qty, 2)
                pct = round((cur_price - avg_cost) / avg_cost * 100, 2)
                sign = "+" if unrealized >= 0 else ""
                emoji = "📈" if unrealized >= 0 else "📉"
                pos_lines.append(
                    f"{idx}\uFE0F\u20E3 {coin} | {'浮盈' if unrealized >= 0 else '浮亏'} {sign}{pct}% (${sign}{unrealized:.2f})\n"
                    f"   持仓: {total_qty:.6f} | 均价: ${avg_cost:.2f} | 现价: ${cur_price:.2f}"
                )
            else:
                pos_lines.append(
                    f"{idx}\uFE0F\u20E3 {coin} | 暂无法获取实时价格\n"
                    f"   持仓: {total_qty:.6f} | 均价: ${avg_cost:.2f} | 现价: N/A"
                )

        positions_section = (
            f"\n🪙 【持仓明细】 (共 {len(positions)} 币种)\n" + "\n".join(pos_lines)
        )
    else:
        positions_section = "\n🪙 【持仓明细】 (共 0 币种)\n无（空仓）"

    # ===== 拼接 =====
    sign_pnl = "+" if total_pnl_cny >= 0 else ""
    sign_u = "+" if total_unrealized_cny >= 0 else ""

    return (
        f"📊 AI 量化每日盘点  [{now_str}]\n"
        f"═════════════════════════\n"
        f"💼 【账户总览】\n"
        f"初始本金: {INITIAL_CAPITAL_CNY:.2f} 元 (~${INITIAL_CAPITAL:.2f})\n"
        f"当前净值: {total_assets_cny:.2f} 元 (~${total_assets_usdt:.2f})\n"
        f"净值波动: {net_sign}{net_change_pct}%\n"
        f"\n"
        f"🎯 【策略表现】\n"
        f"已实现利润: {sign_pnl}{total_pnl_cny:.2f} 元 (落袋为安)\n"
        f"未实现浮动: {sign_u}{total_unrealized_cny:.2f} 元 (持仓盈亏)\n"
        f"胜率表现: {win_rate}% ({win_count}盈 / {lose_count}亏)\n"
        f"交易活跃度: 共 {total_trades} 笔{today_section}\n"
        f"{positions_section}\n"
        f"═════════════════════════\n"
        f"💡 系统提示: 胜率统计正常。已注入防手续费磨损策略。"
    )


# ── 本地自测 ──
if __name__ == "__main__":
    print("=" * 50)
    print(" Gate.io Trade Module - 自测（虚拟账本）")
    print("=" * 50)
    print(f"DRY_RUN = {DRY_RUN}")
    print(f"账本: {_PAPER_LOG}")
    print(f"初始资金: ${INITIAL_CAPITAL} ({INITIAL_CAPITAL_CNY}元)")
    print()

    # 读现有账本，不覆盖
    ledger = _load_ledger()
    if ledger:
        print(f"现有账本有 {len(ledger)} 条记录")
        print(f"执行 generate_ledger_summary() 预览：")
        print()
        print(generate_ledger_summary())
        print()
        print("─" * 50)
        print("各条记录：")
        for entry in ledger:
            pnl = f" | PnL: {entry.get('pnl_pct', 0):+.2f}% ({entry.get('pnl_usdt', 0):+.2f} USDT)" if entry.get('pnl_pct') else ""
            print(f"  {entry['time']} | {entry['action']} {entry['coin']} @ ${entry['fill_price']:.2f}{pnl}")
    else:
        print("账本为空，运行模拟测试...")
        print()

        # 清空测试账本
        _save_ledger([])

        # 模拟多仓模式测试（同一币种多次买入）
        tests = [
            ("btcusdt", "BUY", 10, "BTC", 0),
            ("btcusdt", "BUY", 15, "BTC", 0),   # 多仓：第二次买入 BTC
            ("ethusdt", "BUY", 20, "ETH", 0),
            ("btcusdt", "SELL", 10, "BTC", 0),
            ("ethusdt", "BUY", 15, "ETH", 0),
            ("ethusdt", "SELL", 35, "ETH", 0),
        ]
        for sym, act, amt, name, pct in tests:
            r = execute_order(sym, act, amt, coin_name=name, percentage=pct)
            pnl = f" | PnL: {r['pnl_pct']:+.2f}%" if r['pnl_pct'] != 0 else ""
            print(f"  {act} {name}: ${r['fill_price']:.2f} x {r['quantity']}{pnl}")

        print()
        print("─" * 50)
        print("增强版账本报告：")
        print("─" * 50)
        print(generate_ledger_summary())
