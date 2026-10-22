"""Read-only source inspection, deliberately independent of runtime config."""
import ast
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    names = ("alert_monitor.py", "seven_roles_committee.py", "wechat_server.py",
             "offline_diagnostic.py", "clients/callback_guard.py",
             "clients/gateio_trade.py", "clients/trade_policy.py",
             "clients/decision_log.py")
    checks = []
    for name in names:
        try:
            ast.parse((root / name).read_text(encoding="utf-8"))
            checks.append({"source": name, "syntax_ok": True})
        except (OSError, SyntaxError, UnicodeError):
            checks.append({"source": name, "syntax_ok": False})
    ok = all(item["syntax_ok"] for item in checks)
    print(json.dumps({"mode": "offline_source_check", "ok": ok,
                      "market_scan_performed": False,
                      "account_checked": False, "checks": checks}, ensure_ascii=False))
    return 0 if ok else 2
