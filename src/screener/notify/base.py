"""推播介面（PROJECT_PLAN.md 第 11 節）。job 只依賴這裡的介面，換推播管道時只需新增一個實作。"""

from typing import Protocol


class NotifyError(Exception):
    """推播失敗（重試後仍失敗或被拒絕）。"""


class Notifier(Protocol):
    def send(self, content: str) -> None:
        """送出一則訊息；長度由呼叫端控制在管道的上限之內。"""
        ...
