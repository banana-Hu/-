# wechat-helper

WeFlow DB 监听 + AI 决策 + 桌面操控 三合一微信消息助手。

## 功能

- 🛰 实时监听 WeFlow SSE 推送（< 1秒延迟）
- 🧠 AI 生成回复方案（由 Hermes / Claude / GPT 提供）
- 🎮 PyAutoGUI + Win32 半自动操控微信 PC 客户端
- 🔌 本地 HTTP Bridge，让外部 AI Agent 可以推送方案
- ⌨ 快捷键触发（默认 Ctrl+Shift+S 发送，Esc 取消）
- 💾 SQLite 持久化事件（重启不丢消息）

## 快速开始

```bash
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
# 编辑 config/config.yaml
python -m wechat_helper.main
```

## 架构

见 `README.md` 架构图。

## License

MIT
