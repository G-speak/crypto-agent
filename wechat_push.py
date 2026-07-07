#!/usr/bin/env python3
"""
企业微信推送模块
将分析结果 + 走势图推送到企业微信应用消息
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

# ==================== 配置 ====================
from wechat_config import CORPID, AGENTID, SECRET, TOUSER

BASE_URL = "https://qyapi.weixin.qq.com"

def get_token():
    """获取企业微信 access_token"""
    url = f"{BASE_URL}/cgi-bin/gettoken"
    resp = requests.get(url, params={
        "corpid": CORPID,
        "corpsecret": SECRET
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise Exception(f"获取token失败: {data}")
    return data["access_token"]

def send_text(token, content, touser=TOUSER):
    """发送纯文本消息"""
    url = f"{BASE_URL}/cgi-bin/message/send"
    payload = {
        "touser": touser,
        "msgtype": "text",
        "agentid": AGENTID,
        "text": {
            "content": content
        },
        "safe": 0
    }
    resp = requests.post(url, params={"access_token": token},
                         json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise Exception(f"发送文本消息失败: {data}")
    return data

def push_analysis(analysis_texts, chart_paths, touser=TOUSER):
    """
    推送完整分析到企业微信（所有币种合并成一条消息）
    analysis_texts: list, 每个币种的分析文字
    chart_paths: list, 保留参数但不再使用
    """
    print("  📤 推送中...", end=" ", flush=True)

    try:
        token = get_token()
    except Exception as e:
        print(f"❌ token获取失败: {e}")
        return False

    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")
    merged = f"⏰ {now_str}\n\n" + "\n\n---\n\n".join(analysis_texts)
    try:
        send_text(token, merged, touser)
        print(f"✅", end=" ")
    except Exception as e:
        print(f"❌ Ai消息推送失败: {e}", end=" ")

    print()
    return True

def send_simple_message(content, touser=TOUSER):
    """快速发送一条文本消息"""
    try:
        token = get_token()
        send_text(token, content, touser)
        return True
    except Exception as e:
        print(f"发送失败: {e}")
        return False


if __name__ == "__main__":
    # 测试
    print("测试企业微信推送...")
    ok = send_simple_message("🔮 系统测试消息\n虚拟币监控助手已就绪 ✅")
    print(f"测试结果: {'成功' if ok else '失败'}")
