"""
wechat-helper 主入口
WeFlow DB Watcher + Decision Agent + Desktop Operator

使用方式:
    python -m wechat_helper.main
    python -m wechat_helper.main --module watcher
    python -m wechat_helper.main --module operator
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml

from wechat_helper.utils.logger import setup_logger
from wechat_helper.modules.config_loader import ConfigLoader
from wechat_helper.modules.bridge import Bridge
from wechat_helper.modules.watcher import Watcher
from wechat_helper.modules.operator import Operator


def load_config(config_path: str = "config/config.yaml") -> dict:
    """加载 YAML 配置文件"""
    config_file = Path(config_path)
    if not config_file.exists():
        print(f"❌ Config file not found: {config_path}")
        sys.exit(1)

    with open(config_file, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


async def run_all(config: dict):
    """启动所有模块"""
    logger = logging.getLogger(__name__)
    logger.info("🚀 Starting wechat-helper (all modules)")

    bridge = Bridge(config['bridge'])
    watcher = Watcher(config['watcher'], config['weflow'], bridge)
    operator = Operator(config['operator'], bridge)

    await bridge.start()
    await watcher.start()
    await operator.start()

    logger.info("✅ All modules started. Press Ctrl+C to stop.")

    # 等待终止信号
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("⏹  Stop signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            pass

    await stop_event.wait()

    # 清理
    await operator.stop()
    await watcher.stop()
    await bridge.stop()
    logger.info("👋 wechat-helper stopped")


async def run_module(config: dict, module_name: str):
    """启动指定模块"""
    logger = logging.getLogger(__name__)

    if module_name == "bridge":
        bridge = Bridge(config['bridge'])
        await bridge.start()
        logger.info("✅ Bridge running. Press Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await bridge.stop()

    elif module_name == "watcher":
        bridge = Bridge(config['bridge'])
        await bridge.start()
        watcher = Watcher(config['watcher'], config['weflow'], bridge)
        await watcher.start()
        logger.info("✅ Watcher running. Press Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await watcher.stop()
            await bridge.stop()

    elif module_name == "operator":
        bridge = Bridge(config['bridge'])
        await bridge.start()
        operator = Operator(config['operator'], bridge)
        await operator.start()
        logger.info("✅ Operator running. Press Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await operator.stop()
            await bridge.stop()

    else:
        print(f"❌ Unknown module: {module_name}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="wechat-helper: WeFlow + Decision Agent + Operator")
    parser.add_argument('--config', default='config/config.yaml', help='配置文件路径')
    parser.add_argument('--module', choices=['all', 'watcher', 'operator', 'bridge'],
                        default='all', help='启动哪个模块')
    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)

    # 配置日志
    setup_logger(
        log_dir=config['logging']['log_dir'],
        level=config['logging']['level'],
        max_bytes=config['logging']['max_bytes'],
        backup_count=config['logging']['backup_count'],
    )

    # 运行
    try:
        if args.module == 'all':
            asyncio.run(run_all(config))
        else:
            asyncio.run(run_module(config, args.module))
    except KeyboardInterrupt:
        print("\n👋 Interrupted by user")


if __name__ == "__main__":
    main()
