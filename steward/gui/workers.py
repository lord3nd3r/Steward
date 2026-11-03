"""Background turn so the window stays responsive while a model streams."""

from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from steward.core.models import ApprovalDecision, ApprovalRequest
from steward.core.service import Steward


class TurnWorker(QThread):
    token = Signal(str)
    segment = Signal(str)
    tool_done = Signal(object)
    status = Signal(str)
    need_approval = Signal(object)
    finished_turn = Signal(object)
    failed = Signal(str)

    def __init__(self, steward: Steward, agent_id: str, text: str) -> None:
        super().__init__()
        self.steward = steward
        self.agent_id = agent_id
        self.text = text
        self.cancel_event = threading.Event()
        self._decision: ApprovalDecision | None = None
        self._decision_event = threading.Event()

    def run(self) -> None:
        try:
            result = self.steward.runner.run_turn(
                self.agent_id,
                self.text,
                on_token=self.token.emit,
                on_segment=self.segment.emit,
                on_tool=self.tool_done.emit,
                on_status=self.status.emit,
                approval_handler=self._approve,
                cancel=self.cancel_event,
            )
            self.finished_turn.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _approve(self, request: ApprovalRequest) -> ApprovalDecision:
        self._decision = None
        self._decision_event.clear()
        self.need_approval.emit(request)
        while not self._decision_event.wait(0.1):
            if self.cancel_event.is_set():
                return ApprovalDecision(approved=False)
        return self._decision or ApprovalDecision(approved=False)

    def submit_decision(self, decision: ApprovalDecision) -> None:
        self._decision = decision
        self._decision_event.set()

    def stop(self) -> None:
        self.cancel_event.set()
        self.submit_decision(ApprovalDecision(approved=False))
