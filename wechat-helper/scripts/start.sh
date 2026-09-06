#!/bin/bash
# wechat-helper 启动脚本 (Unix/Git Bash)

cd "$(dirname "$0")/.."

if [ ! -d "venv" ]; then
    echo "[ERROR] 虚拟环境不存在，请先运行: python -m venv venv"
    exit 1
fi

source venv/bin/activate
python -m wechat_helper.main "$@"