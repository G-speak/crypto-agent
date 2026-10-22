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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clients.trade_policy import validate_command, valid_amount
from clients.paper_account import PaperAccount, accounting
from clients.order_journal import number
from datetime import datetime, timezone, timedelta, date
from collections import OrderedDict
from decimal import Decimal

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
INITIAL_CAPITAL_CNY = 500.0       # 初始资金 500 元人民币（沙盘模拟用）
USDT_CNY_RATE = 7.1972            # USDT/CNY 汇率默认值（≈ Gate.io 平台估值汇率，可在 live_account.json 校准）
INITIAL_CAPITAL = round(INITIAL_CAPITAL_CNY / USDT_CNY_RATE, 2)  # ≈ 69 USDT（沙盘模拟用）


def _get_usdt_cny_rate() -> float:
    """
    获取 USDT/CNY 汇率（与 Gate.io 平台估值对齐）。

    优先从 ~/.hermes/live_account.json 的 usdt_cny_rate 字段读取
    （用户可按 Gate.io App 显示的总资产估值校准），
    文件缺失/异常时回退默认值 7.1972。
    """
    try:
        if os.path.exists(_LIVE_CONFIG):
            with open(_LIVE_CONFIG) as f:
                cfg = json.load(f)
            v = float(cfg.get("usdt_cny_rate", 0) or 0)
            if 5 < v < 10:  # 合理性校验
                return v
    except Exception:
        pass
    return USDT_CNY_RATE

# ── 实盘初始本金（真实充值金额）──
DEFAULT_INITIAL_CAPITAL_USDT = 14.71  # 默认值；实际以 ~/.hermes/live_account.json 为准
_LIVE_CONFIG = os.path.expanduser("~/.hermes/live_account.json")


def _get_initial_capital_usdt() -> float:
    """
    读取实盘初始本金（真实充值金额）。

    优先从 ~/.hermes/live_account.json 的 initial_capital_usdt 字段读取
    （充值后可手动更新该文件），文件缺失/异常时回退默认值。
    """
    try:
        if os.path.exists(_LIVE_CONFIG):
            with open(_LIVE_CONFIG) as f:
                cfg = json.load(f)
            v = float(cfg.get("initial_capital_usdt", 0) or 0)
            if v > 0:
                return v
    except Exception:
        pass
    return DEFAULT_INITIAL_CAPITAL_USDT


# ==================== 虚拟账本（DRY_RUN 用）====================

def _paper_account():
    return PaperAccount(_PAPER_LOG + ".state/ledger.sqlite3", INITIAL_CAPITAL, legacy=_PAPER_LOG)


def _load_ledger():
    rows = _paper_account().snapshot()["ledger"]
    # Preserve numeric types for existing report consumers; durable values are decimal strings.
    numeric = ("fill_price", "quantity", "amount_usdt", "fee_usdt", "pnl_usdt", "pnl_pct", "cost_basis")
    return [{k: float(v) if k in numeric else v for k, v in row.items()} for row in rows]


def _save_ledger(ledger):
    raise RuntimeError("直接覆盖账本已禁用；请使用事务式纸面账户")


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


def _calc_pnl(coin_base, action, fill_price, quantity, ledger):
    if action != "SELL":
        return {"pnl_pct": 0.0, "pnl_usdt": 0.0, "cost_basis": 0.0}
    before = accounting(ledger, INITIAL_CAPITAL)
    sale = {"symbol": coin_base+"/USDT", "action": "SELL", "fill_price": fill_price, "quantity": quantity}
    after = accounting(ledger+[sale], INITIAL_CAPITAL)
    pnl = Decimal(after["realized_net"]) - Decimal(before["realized_net"])
    basis = number(fill_price)*number(quantity) - pnl
    return {"pnl_pct": float(pnl/basis*100) if basis else 0.0,
            "pnl_usdt": float(pnl), "cost_basis": float(basis/number(quantity))}


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


def _get_exchange():
    """
    初始化 ccxt Gate.io 交易所对象（实盘用）。

    从 ~/.hermes/gateio.env 读取 API Key。
    返回 exchange 对象；Key 缺失或初始化失败返回 None。
    """
    try:
        import ccxt
    except ImportError:
        _logger.error("ccxt 未安装")
        return None
    env = _load_env()
    api_key = env.get("GATEIO_API_KEY", "")
    api_secret = env.get("GATEIO_API_SECRET", "")
    if not api_key or not api_secret:
        _logger.error("GATEIO_API_KEY 或 GATEIO_API_SECRET 未配置")
        return None
    try:
        exchange = ccxt.gate({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
        })
        return exchange
    except Exception as e:
        _logger.error(f"ccxt Gate.io 初始化失败: {e}")
        return None


def get_real_balance() -> dict:
    """
    获取真实账户余额（Gate.io /spot/accounts，经 ccxt fetch_balance）。

    返回: {"USDT": {"free": 14.71, "used": 0, "total": 14.71}, "BTC": {...}, ...}
    只包含非零余额的币种。失败返回 None。
    """
    exchange = _get_exchange()
    if not exchange:
        return None
    try:
        balance = exchange.fetch_balance()
        result = {}
        for coin, data in balance.items():
            if not isinstance(data, dict):
                continue
            total = float(data.get("total", 0) or 0)
            free = float(data.get("free", 0) or 0)
            used = float(data.get("used", 0) or 0)
            if total > 0 or free > 0 or used > 0:
                result[coin] = {"free": free, "used": used, "total": total}
        return result
    except Exception as e:
        _logger.error(f"获取真实余额失败: {e}")
        return None


def get_real_holdings() -> dict:
    """
    获取真实持仓明细（各币种可用数量）。

    返回: {"BTC": {"quantity": 0.1, "avg_cost": 0, "total_cost": 0}, ...}
    实盘无成本价数据时 avg_cost/total_cost 记 0（仅决策用数量）。
    失败返回空字典。
    """
    balance = get_real_balance()
    if balance is None:
        raise RuntimeError("balance_unavailable")
    holdings = {}
    for coin, data in balance.items():
        if coin == "USDT":
            continue
        qty = float(data.get("total", 0) or 0)
        if qty > 0:
            holdings[coin] = {"quantity": qty, "avg_cost": 0.0, "total_cost": 0.0}
    return holdings


def _get_holdings(ledger: list = None) -> dict:
    """
    获取当前持仓。

    实盘模式（DRY_RUN=False）：读取交易所真实持仓（真金白银）。
    模拟模式（DRY_RUN=True）：从虚拟账本计算。

    返回: {"BTC": {"quantity": 0.1, "avg_cost": 65000, "total_cost": 6500}, ...}
    空仓返回空字典。
    """
    # ── 实盘模式：交易所真实持仓 ──
    if not DRY_RUN:
        return get_real_holdings()

    return _paper_holdings(ledger)


def _paper_holdings(ledger=None):
    """Paper-only holdings; never dispatch based on mutable mode globals."""
    positions = _get_open_positions(ledger)
    holdings = {}
    for coin, info in positions.items():
        holdings[coin] = {
            "quantity": info["total_qty"],
            "avg_cost": info["avg_cost"],
            "total_cost": info["total_cost"],
        }
    return holdings


def _quotes_for(account, extra=()):
    symbols = set(account.snapshot()["positions"]) | set(extra)
    quotes = {}
    for symbol in sorted(symbols):
        price = _fetch_current_price(symbol)
        if valid_amount(price):
            quotes[symbol] = {"price": price, "observed_at": time.time()}
    return quotes


def _paper_trade(ccxt_symbol, coin_name, action, amount_usdt, percentage=0, current_qty=0.0, request_id=None):
    account = _paper_account()
    result = account.place(ccxt_symbol, action, percentage, _quotes_for(account, (ccxt_symbol,)), key=request_id)
    for key in ("pnl_pct", "pnl_usdt", "fill_price", "quantity"):
        result.setdefault(key, 0.0)
    return result


def run_protection_cycle():
    """Independent paper protection. Never invokes AI, notifications or live trading."""
    if DRY_RUN is not True:
        return {"blocked_new_risk": True, "exits": [], "reason": "live_execution_disabled"}
    account = _paper_account()
    return account.protect(_quotes_for(account))


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
                  coin_name: str = "", percentage: int = 0, request_id=None) -> dict:
    """
    执行 Gate.io 市价单（受 DRY_RUN 保护）。

    参数:
        symbol: "btcusdt" / "BTC_USDT" / "BTC/USDT"
        action: "BUY" / "SELL" / "HOLD"
        amount_usdt: 基础下单金额（USDT 计价），默认 10 USDT
                     必须为有限正数；不能用无效 percentage 退回此金额
        coin_name: 币种可读名称（如 "BTC"），用于账本记录；为空时从 symbol 推断
        percentage: 仓位百分比（0-100），BUY时占可用余额，SELL时占持仓量
                    由 alert_monitor / 7角色委员会传入

    返回:
        {
          "success": bool,
          "action": str,
          "detail": str,
          # 仅纸面模式可执行，实盘暂时封锁；纸面返回：
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

    # Live execution deliberately remains unavailable in this staging release.
    # This is an interlock, not authentication against a same-user/root attacker.
    if DRY_RUN is not True:
        result["detail"] = "实盘执行尚未开放：授权执行器与成交核实未验收"
        result["status"] = "blocked_live"
        return result
    try:
        validate_command({"action": action, "percentage": percentage, "reason": "executor validation"})
        if type(symbol) is not str or not symbol.strip():
            raise ValueError("交易对无效")
        if not valid_amount(amount_usdt):
            raise ValueError("金额必须为有限正数")
    except ValueError as exc:
        result["detail"] = str(exc)
        result["status"] = "rejected_input"
        return result

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
    holdings = _paper_holdings()
    current_qty = holdings.get(coin_base, {}).get("quantity", 0.0)

    # 空仓 SELL → 快速拦截（不触发委员会，直接返回）
    if action.upper() == "SELL" and current_qty <= 0:
        msg = f"⛔ 空仓拦截: {coin_name} 当前持仓为0，拒绝执行 SELL（已记录日志）"
        _logger.warning(msg)
        result["detail"] = msg
        result["success"] = False
        result["status"] = "skipped_no_holdings"
        return result

    # 【多仓模式】取消单仓位 BUY 拦截：
    # 同一币种允许分批多次买入，由 7 角色委员会的风控经理决定每次的仓位比例
    # 下⾯不再检查 BUY 时是否已有持仓

    # Budget, FIFO, fees, risk latches and fill recording are one SQLite transaction.
    extra = {"request_id": request_id} if request_id is not None else {}
    trade_result = _paper_trade(ccxt_symbol, coin_name, action, amount_usdt,
                                percentage=percentage, current_qty=current_qty, **extra)
    result.update(trade_result)
    result["status"] = trade_result.get("status", "paper_filled" if result.get("success") else "paper_rejected")
    result["exchange_confirmed"] = False
    return result


# ==================== 账本报告（增强版）====================

def _generate_live_summary(now_str: str) -> str:
    """
    实盘账户状态报告（DRY_RUN=False 时使用）。

    读取真实余额 + 真实持仓 + 实时价格，计算总资产与净值波动。
    本金 = REAL_INITIAL_CAPITAL_USDT（真实充值金额）。
    """
    balance = get_real_balance()
    if not balance:
        return (
            f"📊 AI 量化每日盘点  [{now_str}]\n"
            f"═════════════════════════\n"
            f"⚠️ 实盘账户状态获取失败\n"
            f"请检查 ~/.hermes/gateio.env 的 API 密钥\n"
            f"═════════════════════════\n"
            f"💡 系统提示: 实盘模式运行中。"
        )

    usdt_total = float(balance.get("USDT", {}).get("total", 0) or 0)

    # 真实持仓 + 市值
    holdings = get_real_holdings()
    holdings_value = 0.0
    pos_lines = []
    for coin, info in sorted(holdings.items()):
        qty = float(info.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        cur = _fetch_current_price(f"{coin}/USDT")
        if cur > 0:
            value = qty * cur
            holdings_value += value
            pos_lines.append(f"  {coin}: {qty:.6f} | 现价 ${cur:.2f} | 市值 ${value:.2f}")
        else:
            pos_lines.append(f"  {coin}: {qty:.6f} | 现价 N/A")

    # 总资产 = 可用 USDT + 持仓市值
    total_assets_usdt = round(usdt_total + holdings_value, 2)
    rate = _get_usdt_cny_rate()  # 与 Gate.io 平台估值对齐的汇率
    total_assets_cny = round((usdt_total + holdings_value) * rate, 2)  # 用未舍入 USDT 计算，与 Gate 显示一致

    # 净值波动（相对真实初始本金，用未舍入值计算）
    init_usdt = _get_initial_capital_usdt()
    init_cny = round(init_usdt * rate, 2)
    net_change_pct = round(((usdt_total + holdings_value) - init_usdt) / init_usdt * 100, 2) if init_usdt > 0 else 0.0
    net_sign = "+" if net_change_pct >= 0 else ""

    pos_section = "\n".join(pos_lines) if pos_lines else "无（空仓）"

    return (
        f"📊 AI 量化每日盘点  [{now_str}]\n"
        f"═════════════════════════\n"
        f"💼 【账户总览】\n"
        f"初始本金: {init_usdt:.2f} USDT  (参考 ¥{init_cny:.2f})\n"
        f"当前净值: {total_assets_usdt:.2f} USDT  (参考 ¥{total_assets_cny:.2f})\n"
        f"净值波动: {net_sign}{net_change_pct}%\n"
        f"\n"
        f"🪙 【持仓明细】 (共 {len(holdings)} 币种)\n"
        f"{pos_section}\n"
        f"═════════════════════════\n"
        f"💡 系统提示: 实盘模式运行中。"
    )


def generate_ledger_summary():
    if DRY_RUN is not True:
        return "实盘执行未开放；账户信息请使用只读核实工具。净值变化不等于已实现收益。"
    account = _paper_account()
    snapshot = account.snapshot()
    quotes = _quotes_for(account)
    missing = set(snapshot["positions"]) - set(quotes)
    equity = Decimal(snapshot["cash"])
    lines = ["纸面账户（非真实收益）", f"可用现金: {equity:.4f} USDT",
             f"FIFO 已实现净盈亏: {Decimal(snapshot['realized_net']):+.4f} USDT",
             f"累计模拟手续费: {Decimal(snapshot['fees_usdt']):.4f} USDT"]
    for symbol, pos in snapshot["positions"].items():
        lines.append(f"{symbol}: {pos['quantity']}")
        if symbol in quotes:
            equity += Decimal(pos["quantity"])*Decimal(str(quotes[symbol]["price"]))
    if missing:
        lines.append("行情不全，账户净值未知，禁止将缺失行情当作零。")
    else:
        lines.append(f"估算净值: {equity:.4f} USDT；含持仓浮动，不等于已实现收益。")
    if any(not row.get("paper") for row in snapshot["ledger"]):
        lines.append("含旧账本数据，旧记录可能未计手续费，不能作为完整净收益。")
    lines.append("模拟假设：单边手续费 0.1%、滑点 0.1%；不代表账户实际费率。")
    if snapshot["risk"].get("halted"):
        lines.append("新增风险已熔断，需人工复核后迁移风险状态；不会自动恢复。")
    return "\n".join(lines)


# ── 本地自测 ──
if __name__ == "__main__":
    # P0-1（2026-10-22）：本自测块会连续调用 execute_order 6 次，过去在 DRY_RUN=False 下
    # 等于真实下单（且只要 paper 账本为空就会执行）。现在：默认禁用 + 强制纸面。
    import os as _os
    if _os.environ.get("GATEIO_SELFTEST") != "1":
        print("⛔ 自测块已禁用：该入口会真实下单。设置 GATEIO_SELFTEST=1 才会运行（且强制 DRY_RUN=True）。")
        raise SystemExit(0)
    DRY_RUN = True                    # 强制纸面，绝不触达交易所
    print("=" * 50)
    print(" Gate.io Trade Module - 自测（已强制 DRY_RUN=True，不会实盘下单）")
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
        # New accounts initialize transactionally; existing ledgers are never reset.

        # 模拟多仓模式测试（同一币种多次买入）
        tests = [
            ("btcusdt", "BUY", 10, "BTC", 20),
            ("btcusdt", "BUY", 15, "BTC", 20),   # 多仓：第二次买入 BTC
            ("ethusdt", "BUY", 20, "ETH", 30),
            ("btcusdt", "SELL", 10, "BTC", 100),
            ("ethusdt", "BUY", 15, "ETH", 30),
            ("ethusdt", "SELL", 35, "ETH", 100),
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
