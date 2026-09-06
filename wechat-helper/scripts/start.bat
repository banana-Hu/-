@echo off
chcp 65001 > nul
REM wechat-helper 启动脚本 (Windows)

cd /d %~dp0\..
if not exist venv\Scripts\activate.bat (
    echo [ERROR] 虚拟环境不存在，请先运行: python -m venv venv
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
python -m wechat_helper.main %*