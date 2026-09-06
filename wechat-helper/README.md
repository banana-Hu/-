# wechat-helper

WeFlow DB Watcher + Decision Agent + Desktop Operator

实时监听微信新消息 → AI 生成回复方案 → 桌面操控自动发送。

---

## ⚠️ 免责声明

本工具仅供个人学习与合法商业沟通使用。使用本工具产生的任何后果由使用者自行承担，包括但不限于：
- 微信账号被封禁
- 信息泄露
- 与他人产生的任何纠纷

**请勿用于：**
- 骚扰、诈骗、违法活动
- 未经授权的代发操作
- 任何违反微信用户协议的行为

---

## 🎯 核心功能

1. **实时监听** - 通过 WeFlow 的 SSE 推送接口，实时获取新消息事件
2. **AI 决策** - 由外部 AI Agent（Hermes / Claude / GPT）生成回复方案
3. **桌面执行** - PyAutoGUI + Win32 API 模拟鼠标键盘操作
4. **半自动确认** - 默认需要按快捷键二次确认发送，避免误操作

## 🏗️ 架构

```
┌──────────────┐  SSE   ┌──────────────┐  HTTP   ┌──────────────────┐
│   WeFlow     │ ─────► │   Watcher    │ ──────► │  Decision Agent  │
│ (DB监听+SSE) │        │   (Python)   │         │ (Hermes/Claude)  │
└──────────────┘        └──────────────┘         └──────────────────┘
                                │                         │
                                │                         ▼
                                │                  ┌──────────────┐
                                │                  │   Bridge     │
                                │                  │  (HTTP API)  │
                                │                  └──────────────┘
                                │                         │
                                ▼                         ▼
                        ┌──────────────────────────────────────┐
                        │   Operator (PyAutoGUI + Win32)        │
                        │   激活窗口 → 粘贴 → 发送             │
                        └──────────────────────────────────────┘
                                │
                                ▼
                        ┌──────────────┐
                        │  微信 PC 客户端 │
                        └──────────────┘
```

## 📦 安装

### 前置要求

- Windows 10/11
- Python 3.11+
- [WeFlow](https://github.com/) 已在运行（提供 HTTP API）
- 微信 PC 客户端 4.x

### 安装步骤

```bash
# 克隆仓库
git clone https://github.com/YOUR_USERNAME/wechat-helper.git
cd wechat-helper

# 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 复制配置文件
cp config/config.example.yaml config/config.yaml
# 编辑 config/config.yaml，填入你的 WeFlow 配置
```

## 🚀 使用

### 启动所有模块

```bash
python -m wechat_helper.main
```

### 单独启动某个模块

```bash
# 只启动 Bridge（提供 HTTP API）
python -m wechat_helper.main --module bridge

# 只启动 Watcher（SSE 监听）
python -m wechat_helper.main --module watcher

# 只启动 Operator（桌面操控）
python -m wechat_helper.main --module operator
```

### 操作流程

1. 确保 WeFlow 正在运行（http://127.0.0.1:5030）
2. 打开微信 PC 客户端，确保目标联系人窗口可见
3. 启动 wechat-helper（默认监听所有会话）
4. 等待 Hermes Agent 处理新消息并通过 `POST /api/bridge/inject` 注入回复方案
5. 按 **Ctrl+Shift+S** 发送第一个方案，或编辑后发送

### 快捷键

| 快捷键 | 功能 |
|--------|------|
| Ctrl+Shift+S | 发送第一个回复方案 |
| Esc | 取消当前操作 |

## ⚙️ 配置

编辑 `config/config.yaml`：

```yaml
weflow:
  base_url: http://127.0.0.1:5030
  token: safe:your_token_here

watcher:
  watch_sessions: []  # 空列表 = 监听所有
  # watch_sessions:
  #   - wxid_xxx
  #   - 49559118098@chatroom

operator:
  delay_min: 0.5
  delay_max: 1.5
  require_confirmation: true
```

## 🔌 API 文档

启动 Bridge 后访问 `http://127.0.0.1:5031`：

### GET /health
健康检查

### GET /api/bridge/events?limit=20&status=pending
拉取待处理事件（Hermes Agent 调用）

### GET /api/bridge/event/{event_id}
获取单个事件详情

### POST /api/bridge/inject
推送 AI 生成的回复方案
```json
{
  "event_id": "evt_xxx",
  "suggested_replies": ["方案1", "方案2", "方案3"]
}
```

### GET /api/bridge/status
查询系统状态

### POST /api/bridge/stop
紧急停止所有操作

## 🧪 测试

```bash
pytest tests/
```

## 📊 性能与限制

| 指标 | 数值 |
|------|------|
| SSE 监听延迟 | < 1秒 |
| 操作执行延迟 | 1-3秒 |
| 单条消息处理 | < 500ms |
| 同时监控会话 | 建议 ≤ 20 |
| 风控阈值 | 操作频率 < 5次/分钟 |

## ⚠️ 风控提示

- 不要**频繁发送**相同内容
- 不要在**短时间内**大量操作
- 建议保留**人工确认**环节（默认开启）
- 如触发风控，请立即停止并等待 24 小时

## 🛠️ 故障排查

### WeFlow 连接失败
检查：
1. WeFlow 是否正在运行
2. 端口 5030 是否被占用
3. token 是否正确（在 WeFlow 设置中查看）

### 操作失败
检查：
1. 微信窗口是否在前台
2. 快捷键是否被其他程序占用
3. 是否有管理员权限

### 中文输入失败
安装 pyperclip：
```bash
pip install pyperclip
```

## 📜 License

MIT License

## 🤝 Contributing

欢迎 PR 和 Issue。

## ⚠️ 重要提醒

本项目仅为个人效率工具，**不是微信官方工具**。使用风险自负。
