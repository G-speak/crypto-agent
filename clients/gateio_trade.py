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
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
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


def _get_holdings(ledger: list = None) -> dict:
    """
    从虚拟账本计算当前持仓。
    返回: {"BTC": {"quantity": 0.1, "cost": 65000}, "ETH": {...}}
    空仓返回空字典。
    """
    if ledger is None:
        ledger = _load_ledger()

    holdings = {}
    for entry in ledger:
        coin = entry.get("coin", entry.get("base", ""))
        action = entry.get("action", "")
        qty = entry.get("quantity", 0)
        price = entry.get("fill_price", 0)
        if action == "BUY":
            if coin not in holdings:
                holdings[coin] = {"quantity": 0.0, "total_cost": 0.0}
            holdings[coin]["quantity"] += qty
            holdings[coin]["total_cost"] += qty * price
        elif action == "SELL":
            if coin in holdings:
                held_qty = holdings[coin]["quantity"]
                if qty >= held_qty:
                    del holdings[coin]
                else:
                    holdings[coin]["quantity"] -= qty
                    # 按比例扣减成本
                    ratio = qty / held_qty
                    holdings[coin]["total_cost"] *= (1 - ratio)
    return holdings


def _paper_trade(ccxt_symbol: str, coin_name: str, action: str,
                 amount_usdt: float) -> dict:
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

    msg = (
        f"[DRY RUN] 模拟下单 (已记入虚拟账本)：\n"
        f"  交易对: {ccxt_symbol}\n"
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
                  coin_name: str = "") -> dict:
    """
    执行 Gate.io 市价单（受 DRY_RUN 保护）。

    参数:
        symbol: "btcusdt" / "BTC_USDT" / "BTC/USDT"
        action: "BUY" / "SELL" / "HOLD"
        amount_usdt: 下单金额（USDT 计价），默认 10 USDT
        coin_name: 币种可读名称（如 "BTC"），用于账本记录；为空时从 symbol 推断

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

    # ── 仓位感知拦截（防止空仓SELL / 满仓BUY）──
    coin_base = ccxt_symbol.split("/")[0]
    holdings = _get_holdings()
    current_qty = holdings.get(coin_base, {}).get("quantity", 0.0)

    if action.upper() == "SELL" and current_qty <= 0:
        msg = f"⛔ 空仓拦截: {coin_name} 当前持仓为0，拒绝执行 SELL（已记录日志）"
        _logger.warning(msg)
        result["detail"] = msg
        result["success"] = True  # 不报错，静默拦截
        return result

    if action.upper() == "BUY" and current_qty > 0:
        msg = f"⛔ 仓位拦截: {coin_name} 当前已有持仓 (数量 {current_qty:.8f})，拒绝重复 BUY（已记录日志）"
        _logger.warning(msg)
        result["detail"] = msg
        result["success"] = True
        return result

    # ── DRY_RUN 模式：虚拟账本 ──
    if DRY_RUN:
        trade_result = _paper_trade(ccxt_symbol, coin_name, action, amount_usdt)
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


INITIAL_CAPITAL_CNY = 500.0  # 初始资金 500 元人民币
USDT_CNY_RATE = 7.25  # 当前 USDT/CNY 汇率（约）

def generate_ledger_summary() -> str:
    """
    读取虚拟账本，生成总资产状态文字报告（人民币计价）。
    初始资金: 500 元人民币 ≈ 69 USDT
    """
    ledger = _load_ledger()
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")

    if not ledger:
        return (
            f"📊 ⚖️ AI 模拟盘总账本（人民币）  [{now_str}]\n"
            f"----------------------\n"
            f"💰 初始本金: {INITIAL_CAPITAL_CNY:.0f} 元\n"
            f"📊 当前余额: {INITIAL_CAPITAL_CNY:.0f} 元\n"
            f"📈 累计盈亏: 0.00 元\n"
            f"🔢 交易次数: 0 次\n"
            f"🪙 持仓: 无（空仓）\n"
            f"----------------------\n"
            f"等待 AI 策略触发首次交易信号..."
        )

    total_trades = len(ledger)
    sell_records = [e for e in ledger if e.get("action") == "SELL" and e.get("pnl_pct") is not None]
    total_pnl_usdt = round(sum(e.get("pnl_usdt", 0) for e in sell_records), 2)
    total_pnl_cny = round(total_pnl_usdt * USDT_CNY_RATE, 2)
    current_balance_cny = round(INITIAL_CAPITAL_CNY + total_pnl_cny, 2)

    win_count = sum(1 for e in sell_records if e.get("pnl_usdt", 0) > 0)
    win_rate = round(win_count / len(sell_records) * 100, 1) if sell_records else 0.0

    # 当前持仓
    bought = set()
    for e in ledger:
        if e.get("action") == "BUY":
            bought.add(e.get("coin", e.get("base", "")))
        elif e.get("action") == "SELL":
            coin = e.get("coin", e.get("base", ""))
            bought.discard(coin)
    holdings = ", ".join(sorted(bought)) if bought else "无（空仓）"

    sign = "+" if total_pnl_cny >= 0 else ""
    emoji = "📈" if total_pnl_cny >= 0 else "📉"

    return (
        f"📊 ⚖️ AI 模拟盘总账本（人民币）  [{now_str}]\n"
        f"----------------------\n"
        f"💰 初始本金: {INITIAL_CAPITAL_CNY:.0f} 元\n"
        f"📊 当前余额: {current_balance_cny:.2f} 元\n"
        f"{emoji} 累计盈亏: {sign}{total_pnl_cny:.2f} 元\n"
        f"🎯 胜率: {win_rate}% ({win_count}/{len(sell_records)} 笔盈利)\n"
        f"🔢 交易次数: {total_trades} 次\n"
        f"🪙 持仓: {holdings}\n"
        f"----------------------\n"
        f"(数据每逢大盘异动自动更新)"
    )


# ── 本地自测 ──
if __name__ == "__main__":
    print("=" * 50)
    print(" Gate.io Trade Module - 自测（虚拟账本）")
    print("=" * 50)
    print(f"DRY_RUN = {DRY_RUN}")
    print(f"账本: {_PAPER_LOG}")
    print()

    # 清空测试账本
    _save_ledger([])

    # 模拟流程：BTC 买入 → BTC 卖出（应有盈亏）→ ETH 买入 → ETH 买入 → ETH 卖出
    tests = [
        ("btcusdt", "BUY", 10, "BTC"),
        ("ethusdt", "BUY", 20, "ETH"),
        ("btcusdt", "SELL", 10, "BTC"),
        ("ethusdt", "BUY", 15, "ETH"),
        ("ethusdt", "SELL", 35, "ETH"),
    ]
    for sym, act, amt, name in tests:
        r = execute_order(sym, act, amt, coin_name=name)
        pnl = f" | PnL: {r['pnl_pct']:+.2f}%" if r['pnl_pct'] != 0 else ""
        print(f"  {act} {name}: ${r['fill_price']:.2f} x {r['quantity']}{pnl}")

    print()
    print("─" * 50)
    print("虚拟账本内容：")
    print("─" * 50)
    ledger = _load_ledger()
    for entry in ledger:
        pnl = f" | PnL: {entry.get('pnl_pct', 0):+.2f}% ({entry.get('pnl_usdt', 0):+.2f} USDT)" if entry.get('pnl_pct') else ""
        print(f"  {entry['time']} | {entry['action']} {entry['coin']} @ ${entry['fill_price']:.2f}{pnl}")
