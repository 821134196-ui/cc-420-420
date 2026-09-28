"""进程内定时任务：周期扫描临时卡到期并自动失效。"""
from __future__ import annotations

import threading
import time

from .services import Service


class ExpiryScheduler(threading.Thread):
    """守护线程：每 interval 秒执行一次临时卡到期处理。"""

    def __init__(self, service: Service, interval: float = 10.0):
        super().__init__(daemon=True, name="expiry-scheduler")
        self.service = service
        self.interval = interval
        self._stop = threading.Event()
        self.history: list[str] = []

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                expired = self.service.expire_temporary_cards()
                if expired:
                    self.history.extend(expired)
            except Exception as exc:  # 定时任务不允许拖垮进程
                print(f"[expiry-scheduler] 执行失败: {exc}")

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> list[str]:
        expired = self.service.expire_temporary_cards()
        self.history.extend(expired)
        return expired
