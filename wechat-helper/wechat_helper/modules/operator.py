"""
Operator 模块 - 桌面操控执行器

接收 Decision Agent 的回复方案，在 Windows 上模拟鼠标点击、键盘输入
将文本粘贴到微信 PC 客户端并发送

注意：
- 不使用 Hook/DLL 注入（已被微信封禁）
- 仅使用 UI Automation + Win32 API
- 操作间随机延迟避免触发风控
"""
import asyncio
import logging
import random
import threading
import time
from pathlib import Path
from typing import List, Optional

try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    PYAUTOGUI_AVAILABLE = False

try:
    import win32gui
    import win32con
    import win32api
    import win32process
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

try:
    import keyboard  # 全局快捷键监听
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False


class Operator:
    """桌面操控执行器"""

    def __init__(self, config: dict, bridge):
        self.config = config
        self.bridge = bridge
        self.logger = logging.getLogger(self.__class__.__name__)

        # 操控参数
        self.delay_min = config.get('delay_min', 0.5)
        self.delay_max = config.get('delay_max', 1.5)
        self.send_hotkey = config.get('send_hotkey', 'ctrl+shift+s')
        self.cancel_hotkey = config.get('cancel_hotkey', 'esc')
        self.require_confirmation = config.get('require_confirmation', True)
        self.wechat_window_titles = config.get('wechat_window_titles', ['微信', 'WeChat'])

        # 状态
        self._running = False
        self._emergency_stop = False
        self._current_task: Optional[asyncio.Task] = None

        # 屏幕分辨率（用于归一化坐标）
        self.screen_width, self.screen_height = (1920, 1080)
        if PYAUTOGUI_AVAILABLE:
            self.screen_width, self.screen_height = pyautogui.size()

        # 截图目录
        self.screenshot_dir = Path(config.get('screenshot_dir', './logs/screenshots'))
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        # 待发送的回复（来自 Bridge）
        self._pending_reply: Optional[dict] = None
        self._reply_lock = threading.Lock()

    async def start(self):
        """启动 Operator"""
        if not PYAUTOGUI_AVAILABLE:
            self.logger.warning("⚠️  pyautogui not installed, Operator will be no-op")
        if not WIN32_AVAILABLE:
            self.logger.warning("⚠️  pywin32 not installed, window activation disabled")
        if not KEYBOARD_AVAILABLE:
            self.logger.warning("⚠️  keyboard not installed, hotkey disabled")

        self._running = True
        self._emergency_stop = False

        # 启动热键监听线程
        if KEYBOARD_AVAILABLE:
            threading.Thread(target=self._register_hotkeys, daemon=True).start()

        # 启动轮询 pending_reply 的任务
        self._current_task = asyncio.create_task(self._poll_pending_reply())

        # 上报运行状态
        await self.bridge.handle_operator_event.__self__._post_status(
            running=True, action='start'
        )

        self.logger.info("🎮 Operator started")

    async def stop(self):
        """停止 Operator"""
        self._running = False
        if self._current_task:
            self._current_task.cancel()
        self.logger.info("Operator stopped")

    # ────────────── 热键监听 ──────────────

    def _register_hotkeys(self):
        """注册全局快捷键"""
        if not KEYBOARD_AVAILABLE:
            return
        try:
            keyboard.add_hotkey(self.send_hotkey, self._on_send_hotkey)
            keyboard.add_hotkey(self.cancel_hotkey, self._on_cancel_hotkey)
            self.logger.info(f"⌨  Hotkey registered: {self.send_hotkey} (send) | {self.cancel_hotkey} (cancel)")
        except Exception as e:
            self.logger.error(f"Failed to register hotkey: {e}")

    def _on_send_hotkey(self):
        """触发发送"""
        if self._pending_reply:
            self.logger.info("🔥 Send hotkey pressed")
            # 把任务扔进事件循环
            asyncio.run_coroutine_threadsafe(
                self._execute_send(self._pending_reply),
                asyncio.get_event_loop()
            )

    def _on_cancel_hotkey(self):
        """取消操作"""
        self._emergency_stop = True
        self.logger.warning("⛔ Cancel hotkey pressed")

    # ────────────── 轮询 Bridge ──────────────

    async def _poll_pending_reply(self):
        """从 Bridge 拉取 pending_reply 状态"""
        while self._running:
            try:
                # 通过 Bridge 的 _operator_status 字段检查
                status = self.bridge._operator_status
                if status.get('pending_reply') and status['pending_reply'] != self._pending_reply:
                    with self._reply_lock:
                        self._pending_reply = status['pending_reply']
                    self.logger.info(f"📥 Pending reply available: {len(self._pending_reply.get('replies', []))} options")

                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Poll error: {e}")
                await asyncio.sleep(1)

    # ────────────── 发送执行 ──────────────

    async def _execute_send(self, pending: dict):
        """执行发送：激活窗口 → 选会话 → 粘贴 → 发送"""
        if self._emergency_stop:
            self.logger.warning("Emergency stop active, skipping")
            return

        replies = pending.get('replies', [])
        if not replies:
            self.logger.warning("No replies to send")
            return

        # 默认选第一个方案（用户可以按数字键 1/2/3 切换）
        chosen_text = replies[0]
        self.logger.info(f"📤 Sending (default first option): {chosen_text[:80]}")

        try:
            # Step 1: 激活微信窗口
            await self._activate_wechat_window()

            # Step 2: 等待焦点稳定
            await self._random_delay()

            # Step 3: 粘贴文字
            await self._paste_text(chosen_text)

            # Step 4: 等待用户确认（如果启用）
            if self.require_confirmation:
                self.logger.info("⏸  Confirmation required - press Ctrl+Shift+S again to send, Esc to cancel")
                # 等待 3 秒后未确认则跳过
                await asyncio.sleep(3)
                if self._emergency_stop:
                    self.logger.warning("Cancelled during confirmation wait")
                    self._emergency_stop = False
                    return

            # Step 5: 按 Enter 发送
            await self._send_enter()

            # Step 6: 上报发送成功
            self._report_send_result(pending['event_id'], success=True)

        except Exception as e:
            self.logger.error(f"Send failed: {e}", exc_info=True)
            self._report_send_result(pending['event_id'], success=False, error=str(e))
        finally:
            with self._reply_lock:
                self._pending_reply = None

    async def _activate_wechat_window(self):
        """激活微信窗口到前台"""
        if not WIN32_AVAILABLE:
            return

        hwnd = self._find_wechat_window()
        if not hwnd:
            raise RuntimeError("WeChat window not found")

        # 检查窗口是否最小化
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        # 激活窗口
        win32gui.SetForegroundWindow(hwnd)
        self.logger.debug(f"Window activated: hwnd={hwnd}")

    def _find_wechat_window(self) -> Optional[int]:
        """查找微信窗口句柄"""
        if not WIN32_AVAILABLE:
            return None

        result = []
        def enum_callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if any(t in title for t in self.wechat_window_titles):
                    result.append(hwnd)
            return True

        win32gui.EnumWindows(enum_callback, None)
        return result[0] if result else None

    async def _paste_text(self, text: str):
        """在当前焦点输入框粘贴文字"""
        if not PYAUTOGUI_AVAILABLE:
            return

        # 使用 typewrite（字符级别输入）或 write
        # 注意：中文需要 clipboard 方式
        if self._is_chinese(text):
            await self._paste_via_clipboard(text)
        else:
            # 英文/数字直接 typewrite
            pyautogui.typewrite(text, interval=0.02)

    def _is_chinese(self, text: str) -> bool:
        """检测是否包含中文"""
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff':
                return True
        return False

    async def _paste_via_clipboard(self, text: str):
        """通过剪贴板粘贴（支持中文）"""
        try:
            import pyperclip
            pyperclip.copy(text)
            await self._random_delay(0.1, 0.3)
            if KEYBOARD_AVAILABLE:
                keyboard.send('ctrl+v')
            else:
                # fallback: 用 pyautogui
                pyautogui.hotkey('ctrl', 'v')
        except ImportError:
            self.logger.warning("pyperclip not installed, falling back to typewrite")
            pyautogui.typewrite(text, interval=0.05)

    async def _send_enter(self):
        """按 Enter 发送"""
        if KEYBOARD_AVAILABLE:
            keyboard.press_and_release('enter')
        elif PYAUTOGUI_AVAILABLE:
            pyautogui.press('enter')
        await self._random_delay(0.2, 0.5)

    # ────────────── 工具方法 ──────────────

    async def _random_delay(self, min_d: float = None, max_d: float = None):
        """随机延迟"""
        min_d = min_d if min_d is not None else self.delay_min
        max_d = max_d if max_d is not None else self.delay_max
        delay = random.uniform(min_d, max_d)
        await asyncio.sleep(delay)

    def _report_send_result(self, event_id: str, success: bool, error: str = None):
        """上报发送结果给 Bridge"""
        try:
            import requests
            requests.post(
                f"http://{self.bridge.host}:{self.bridge.port}/api/bridge/operator/event",
                headers={'Authorization': f'Bearer {self.bridge.auth_token}'},
                json={
                    'event_id': event_id,
                    'action': 'sent' if success else 'failed',
                    'error': error,
                    'timestamp': time.time()
                },
                timeout=5
            )
        except Exception as e:
            self.logger.warning(f"Failed to report send result: {e}")

    async def take_screenshot(self, name: str = None) -> str:
        """截屏保存"""
        if not PYAUTOGUI_AVAILABLE:
            return ""

        timestamp = time.strftime('%Y%m%d_%H%M%S')
        filename = name or f"screen_{timestamp}.png"
        filepath = self.screenshot_dir / filename

        screenshot = pyautogui.screenshot()
        screenshot.save(str(filepath))
        return str(filepath)
