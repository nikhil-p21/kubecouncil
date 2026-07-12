"""Component-level readiness used to gate human Approval."""

from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class ReadinessCheck:
    ready: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"ready": self.ready, "detail": self.detail}


class ReadinessRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, ReadinessCheck] = {}
        self._lock = RLock()

    def set(self, name: str, *, ready: bool, detail: str) -> None:
        with self._lock:
            self._checks[name] = ReadinessCheck(ready=ready, detail=detail)

    def snapshot(self) -> dict[str, ReadinessCheck]:
        with self._lock:
            return self._checks.copy()

    @property
    def ready(self) -> bool:
        checks = self.snapshot()
        return bool(checks) and all(check.ready for check in checks.values())

    @property
    def approval_enabled(self) -> bool:
        checks = self.snapshot()
        required = ("identity", "incident_store", "evidence", "council", "intervention")
        return all(checks.get(name, ReadinessCheck(False, "missing")).ready for name in required)
