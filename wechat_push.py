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
CORPID = "ww3f126a4c3cb3774d"  # 企业ID (从后台"我的企业"→"企业信息"获取)
AGENTID = 1000002
SECRET = "4cunw-RUUJwIgm0_l4-e48H_C-dfP1_OwphFsVP6o0o"

# 推送目标用户 (你的企业微信账号，后台"通讯录"可以看到)
# 如果不填, 可以填 "@all" 推给所有人, 但建议填你的账号名
TOUSER = "@all"

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


def upload_image(token, image_path):
    """上传图片到企业微信临时素材，返回 media_id"""
    url = f"{BASE_URL}/cgi-bin/media/upload"
    resp = requests.post(url, params={
        "access_token": token,
        "type": "image"
    }, files={
        "media": (os.path.basename(image_path), open(image_path, "rb"), "image/png")
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise Exception(f"上传图片失败: {data}")
    return data["media_id"]


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


def send_image(token, media_id, touser=TOUSER):
    """发送图片消息"""
    url = f"{BASE_URL}/cgi-bin/message/send"
    payload = {
        "touser": touser,
        "msgtype": "image",
        "agentid": AGENTID,
        "image": {
            "media_id": media_id
        },
        "safe": 0
    }
    resp = requests.post(url, params={"access_token": token},
                         json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise Exception(f"发送图片消息失败: {data}")
    return data


def push_analysis(analysis_texts, chart_paths, touser=TOUSER):
    """
    推送完整分析到企业微信
    analysis_texts: list, 每个币种的分析文字
    chart_paths: list, 每个币种的走势图路径
    """
    print("  📤 推送中...", end=" ", flush=True)

    try:
        token = get_token()
    except Exception as e:
        print(f"❌ token获取失败: {e}")
        return False

    # 上传所有图片
    media_ids = []
    for path in chart_paths:
        if not path or not os.path.exists(path):
            media_ids.append(None)
            continue
        try:
            mid = upload_image(token, path)
            media_ids.append(mid)
        except Exception as e:
            print(f"⚠️ 图片上传失败 {path}: {e}")
            media_ids.append(None)

    # 逐币种推送: 先发图, 再发文字
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")
    for i, (text, mid) in enumerate(zip(analysis_texts, media_ids)):
        try:
            if mid:
                time.sleep(0.5)
                send_image(token, mid, touser)
            time.sleep(0.5)
            text_with_time = f"⏰ {now_str}\n{text}"
            send_text(token, text_with_time, touser)
            print(f"✅", end=" ")
        except Exception as e:
            print(f"❌ Ai消息推送失败: {e}", end=" ")

    print()
    return True


def send_simple_message(content, touser=TOUSER):
    """快速发送一条文本消息 (自动加上时间戳)"""
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")
    content = f"⏰ {now_str}\n{content}"
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
