"""
logger 工具
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(log_dir: str, level: str = "INFO",
                 max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5):
    """配置日志系统，同时输出到文件和控制台"""
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # 根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清除已有 handler
    root_logger.handlers.clear()

    # 格式
    formatter = logging.Formatter(
        '[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 文件 handler（轮转）
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, 'wechat-helper.log'),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 降低 httpx 噪音
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取模块 logger"""
    return logging.getLogger(name)
