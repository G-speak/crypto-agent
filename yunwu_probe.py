#!/usr/bin/env python3
"""
yunwu.ai 连通性定时探测脚本
每天 0/6/12/18 点由 crontab 调用，记录 yunwu 是否可用。

日志文件: ~/.hermes/logs/yunwu_probe.log
用法: python3 yunwu_probe.py
"""
import os
import sys
import json
import requests as _req
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG_FILE = os.path.expanduser("~/.hermes/logs/yunwu_probe.log")
STATE_FILE = os.path.expanduser("~/.hermes/yunwu_state.json")


def _bjt_now_str() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def probe_yunwu(timeout: float = 8.0) -> dict:
    """发最小 chat 请求探测 yunwu，返回 {ok, code, detail}"""
    try:
        from wechat_config import YUNWU_API_KEY
        if not YUNWU_API_KEY:
            return {"ok": False, "code": -1, "detail": "YUNWU_API_KEY 未配置"}
        resp = _req.post(
            "https://yunwu.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {YUNWU_API_KEY}"},
            json={
                "model": "deepseek-v3.2",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
                "temperature": 0,
            },
            timeout=timeout,
        )
        if resp.status_code == 200:
            return {"ok": True, "code": 200, "detail": "正常"}
        return {"ok": False, "code": resp.status_code, "detail": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "code": -2, "detail": str(e)[:100]}


def main():
    now_str = _bjt_now_str()
    result = probe_yunwu()
    status_cn = "✅ 可用" if result["ok"] else "❌ 不可用"

    # 追加日志
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{now_str}] yunwu {status_cn} (code={result['code']}) {result['detail']}\n")

    # 更新状态文件（记录每次探测结果，供后续恢复判断）
    try:
        state = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
        state["last_probe"] = now_str
        state["last_ok"] = result["ok"]
        state["last_detail"] = result["detail"]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    print(f"[{now_str}] yunwu {status_cn} (code={result['code']}) {result['detail']}")


if __name__ == "__main__":
    main()
