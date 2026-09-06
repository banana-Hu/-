"""
手动注入测试回复（开发调试用）

用法:
    python scripts/test_inject.py <event_id> <reply_text>
"""
import asyncio
import sys
import aiohttp


async def inject(token: str, event_id: str, replies: list, host='127.0.0.1', port=5031):
    url = f"http://{host}:{port}/api/bridge/inject"
    headers = {'Authorization': f'Bearer {token}'}
    payload = {
        'event_id': event_id,
        'suggested_replies': replies
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            print(f"Status: {resp.status}")
            print(await resp.text())


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python test_inject.py <event_id> <reply1> [reply3] ...")
        sys.exit(1)

    event_id = sys.argv[1]
    replies = sys.argv[2:]
    token = "wechat-helper-secret-2026"  # 从 config.yaml 中获取

    asyncio.run(inject(token, event_id, replies))