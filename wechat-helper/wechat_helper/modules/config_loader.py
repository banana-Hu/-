"""
配置加载工具
"""
from pathlib import Path
from typing import Any, Dict

import yaml


class ConfigLoader:
    """配置加载器，支持热重载"""

    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = {}
        self._last_mtime = 0.0
        self.load()

    def load(self) -> Dict[str, Any]:
        """从 YAML 文件加载配置"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        mtime = self.config_path.stat().st_mtime
        if mtime != self._last_mtime or not self._config:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
            self._last_mtime = mtime

        return self._config

    @property
    def raw(self) -> Dict[str, Any]:
        """返回最新配置（自动检测文件变化）"""
        return self.load()

    def get(self, key_path: str, default: Any = None) -> Any:
        """
        通过点号分隔的路径获取配置项
        例如: get('weflow.base_url')
        """
        keys = key_path.split('.')
        value = self.raw
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value
