"""
WeFlow HTTP API 客户端

封装与 WeFlow 127.0.0.1:5030 的 HTTP/SSE 通信
"""
import asyncio
import json
import logging
from typing import AsyncIterator, Dict, List, Optional

import httpx


class WeFlowClient:
    """WeFlow HTTP API 客户端"""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip('/')
        self.api_path = '/api/v1'
        self.token = token.replace('safe:', '')  # 去掉 safe: 前缀
        self.timeout = timeout
        self.logger = logging.getLogger(self.__class__.__name__)
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    'Authorization': f'Bearer {self.token}',
                    'Accept': 'application/json',
                    'User-Agent': 'wechat-helper/1.0'
                }
            )

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self):
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def get_sessions(self, limit: int = 200, offset: int = 0) -> List[Dict]:
        """获取会话列表"""
        await self._ensure_client()
        url = f"{self.base_url}{self.api_path}/sessions"
        params = {'limit': limit, 'offset': offset}
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get('sessions', [])
        except Exception as e:
            self.logger.error(f"get_sessions failed: {e}")
            return []

    async def get_messages(self, talker: str, limit: int = 50, offset: int = 0) -> List[Dict]:
        """获取某会话的消息"""
        await self._ensure_client()
        url = f"{self.base_url}{self.api_path}/messages"
        params = {'talker': talker, 'limit': limit, 'offset': offset}
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get('messages', [])
        except Exception as e:
            self.logger.error(f"get_messages({talker}) failed: {e}")
            return []

    async def subscribe_push_events(self) -> AsyncIterator[Dict]:
        """
        订阅 WeFlow 的 SSE 推送流

        WeFlow 会通过 /push/messages 端点推送新消息事件
        事件格式: event: <type>\ndata: <json>\n\n
        """
        await self._ensure_client()
        url = f"{self.base_url}{self.api_path}/push/messages"
        self.logger.info(f"Subscribing to SSE: {url}")

        backoff = 1
        while True:
            try:
                async with self._client.stream('GET', url) as resp:
                    resp.raise_for_status()
                    self.logger.info(f"✅ SSE connected: {resp.status_code}")
                    backoff = 1  # 连接成功，重置退避

                    event_type = "message"
                    data_buffer = []

                    async for line in resp.aiter_lines():
                        if line is None:
                            continue

                        if line.startswith('event:'):
                            event_type = line[6:].strip()
                        elif line.startswith('data:'):
                            data_buffer.append(line[5:].strip())
                        elif line == '':
                            # 空行表示事件结束
                            if data_buffer:
                                data_str = '\n'.join(data_buffer)
                                data_buffer = []
                                try:
                                    payload = json.loads(data_str)
                                    yield {
                                        'event': event_type,
                                        'data': payload
                                    }
                                except json.JSONDecodeError as e:
                                    self.logger.warning(f"Failed to parse SSE data: {e}")
                                    yield {
                                        'event': event_type,
                                        'data': {'raw': data_str}
                                    }

                    self.logger.warning("SSE stream ended, reconnecting...")
            except httpx.HTTPStatusError as e:
                self.logger.error(f"SSE HTTP error: {e.response.status_code}")
            except httpx.ConnectError as e:
                self.logger.error(f"SSE connection failed: {e}")
            except Exception as e:
                self.logger.error(f"SSE unexpected error: {e}")

            # 指数退避
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# 测试用
if __name__ == "__main__":
    async def test():
        # 默认从 WeFlow-config.json 读取
        import json as _json
        from pathlib import Path as _Path
        cfg_path = _Path(r"C:\Users\hu\AppData\Roaming\weflow\WeFlow-config.json")
        if cfg_path.exists():
            cfg = _json.loads(cfg_path.read_text(encoding='utf-8'))
            base = f"http://{cfg['httpApiHost']}:{cfg['httpApiPort']}"
            token = cfg['httpApiToken']
        else:
            print("No WeFlow config found")
            return

        async with WeFlowClient(base, token) as client:
            sessions = await client.get_sessions(limit=5)
            print(f"Got {len(sessions)} sessions")
            for s in sessions[:5]:
                print(f"  - {s.get('displayName')}")

    asyncio.run(test())
