#!/usr/bin/env python3
"""
健康检查 + 邮件告警模块
定期检查：API余额、进程存活
故障时发送邮件到管理员邮箱，同一故障1小时内不重复告警
"""
import os, sys, time, json, smtplib
import traceback
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(__file__))

# ====== 配置 ======
ALARM_EMAIL = "1127923801@qq.com"
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587
SMTP_USER = "1127923801@qq.com"
SMTP_PASS = "hwfzvakwjhwkgcej"

COOLDOWN_FILE = os.path.expanduser("~/.hermes/alert_cooldown.json")
LOG_FILE = os.path.expanduser("~/.hermes/logs/alert_monitor.log")
ALARM_COOLDOWN = 3600  # 同一告警类型1小时内不重复

HEALTH_FILE = os.path.expanduser("~/.hermes/health_state.json")

def log(msg):
    t = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{t}] [健康检查] {msg}"
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

def send_email(subject, body):
    """发送告警邮件"""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = SMTP_USER
        msg["To"] = ALARM_EMAIL
        msg["Subject"] = subject

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

        log(f"告警邮件已发送: {subject}")
        return True
    except Exception as e:
        log(f"发送邮件失败: {e}")
        return False

def check_and_alarm(alarm_type, subject, body):
    """检查冷却+发送告警，返回True/False"""
    cooldown = load_json(COOLDOWN_FILE)
    now = time.time()
    key = f"health:{alarm_type}"
    if key in cooldown and now - cooldown[key] < ALARM_COOLDOWN:
        return False  # 冷却中，跳过
    if send_email(subject, body):
        cooldown[key] = now
        save_json(COOLDOWN_FILE, cooldown)
        return True
    return False

def check_api_balance():
    """检查DeepSeek余额是否不足"""
    try:
        from wechat_config import DEEPSEEK_API_KEY
        import requests
        resp = requests.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            timeout=10
        )
        if resp.status_code == 200:
            info = resp.json().get("balance_infos", [])
            balance = info[0].get("total_balance", "0") if info else "0"
            bal_f = float(balance)
            if bal_f < 1.0:
                check_and_alarm(
                    "balance_low",
                    "[AI币助手] DeepSeek余额不足",
                    "当前余额: ¥" + balance + "\n建议及时充值，否则AI分析将无法正常使用。\n时间: " + datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                )
            elif bal_f < 5.0:
                log(f"余额较低: ¥{balance}")
            return True
        elif resp.status_code == 401:
            check_and_alarm(
                "api_401",
                "[AI币助手] DeepSeek API Key 失效(401)",
                "DeepSeek API返回401，请检查API Key是否正确。\n时间: " + datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            )
            return False
        else:
            log(f"DeepSeek余额查询异常: HTTP {resp.status_code}")
            return False
    except Exception as e:
        log(f"余额检查异常: {e}")
        return False

def check_processes():
    """检查三个服务进程是否存活"""
    import subprocess
    services = {
        "server_monitor.py": "定时推送服务",
        "wechat_server.py": "企业微信回调服务",
        "alert_monitor.py": "实时异动监控服务"
    }
    all_ok = True
    for proc_name, label in services.items():
        try:
            result = subprocess.run(
                ["pgrep", "-f", proc_name],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                all_ok = False
                check_and_alarm(
                    "process_down_" + proc_name,
                    "[AI币助手] " + label + " 已停止运行",
                    "进程 " + proc_name + " 未在运行，请登录服务器检查。\n建议: cd /root/crypto_agent && python3 -u " + proc_name + " &\n时间: " + datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                )
        except Exception as e:
            log(f"检查进程 {proc_name} 失败: {e}")
    if all_ok:
        log("所有进程运行正常")
    return all_ok

def health_check():
    """执行一次全面健康检查，返回(ok, details)"""
    issues = []
    
    # 1. 检查API余额
    if not check_api_balance():
        issues.append("DeepSeek余额检查异常")
    
    # 2. 检查进程
    if not check_processes():
        issues.append("有进程未运行")
    
    # 3. 记录健康状态
    state = load_json(HEALTH_FILE)
    state["last_check"] = time.time()
    state["ok"] = len(issues) == 0
    state["issues"] = issues
    save_json(HEALTH_FILE, state)
    
    return len(issues) == 0, issues

if __name__ == "__main__":
    ok, issues = health_check()
    if ok:
        print("健康检查通过")
    else:
        print("发现问题: " + ", ".join(issues))
