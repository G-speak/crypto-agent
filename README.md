# AI 币助手 🤖💰

加密货币监控 + AI 分析机器人，通过企业微信推送实时行情分析和价格异动预警。

## 功能

- **定时推送** — 每天 08:00 / 20:00 推送 BTC、ETH、SOL、BNB 的技术面分析（含走势图）
- **实时异动监控** — 每 5 分钟检查价格波动、RSI 超卖超买、布林带突破
- **AI 智能分析** — 自动路由：日常分析走 DeepSeek，实时新闻走 Grok，带搜索功能
- **自动补仓/卖出价位提醒** — 用户可设置价格预警

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的 Key
vim .env
```

需要申请：
1. **企业微信应用** — 在 [work.weixin.qq.com](https://work.weixin.qq.com) 后台创建应用，获取 CORPID、SECRET、TOKEN、ENCODING_AES_KEY
2. **AIHubMix Key** — 在 [aihubmix.com](https://aihubmix.com) 注册，充值后获取 API Key
3. **DeepSeek Key** — 在 [platform.deepseek.com](https://platform.deepseek.com) 注册获取

### 2. 安装依赖

```bash
pip install requests matplotlib numpy cryptography flask
```

### 3. 启动服务

```bash
# 加载环境变量
export $(grep -v '^#' .env | xargs)

# 启动定时推送 + 预警监控
python3 server_monitor.py

# 启动企业微信回调（用户发消息自动回复）
python3 wechat_server.py
```

## 优惠活动

本项目已集成 **AIHubMix** 作为模型供应商。如果你也想享受 10% 计费优惠：

1. 登录 [aihubmix.com/appstore](https://aihubmix.com/appstore) 申请应用 Code
2. 在环境变量中设置 `AIHUBMIX_APP_CODE=你的6位Code`
3. 调用 AIHubMix API 时自动带上 `APP-Code` 请求头

适用模型：所有模型（除 Claude 系列），请求计费 10% 优惠。

## 项目结构

```
├── wechat_config.py      # 配置（从环境变量读取）
├── wechat_push.py         # 企业微信消息推送
├── wechat_server.py       # 企业微信回调服务 (Flask)
├── crypto_monitor.py      # 行情数据获取 + AI 分析
├── alert_monitor.py       # 实时异动监控
├── price_alerts.py        # 用户自定义价格预警
├── server_monitor.py      # 服务启动入口
├── run_monitor.py         # 定时推送循环
└── .env.example           # 环境变量模板
```
