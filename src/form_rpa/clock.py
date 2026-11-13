"""测试与演练用的可控时钟，不依赖真实 sleep 即可快进租约时间。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class FakeClock:
    """从固定起点开始，可任意快进的时钟；可直接作为 ReceiptStore 的 clock。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 11, 13, 9, 0, 0,
                                          tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)
