from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class CleanupError(RuntimeError):
    """Raised when expired rehearsal cleanup cannot inspect or delete namespaces."""


@dataclass(frozen=True)
class CleanupResult:
    inspected: int
    deleted: tuple[str, ...]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, arguments: Sequence[str]) -> CommandResult:
        ...


class RehearsalNamespaceCleaner:
    def __init__(
        self,
        command: Sequence[str] = ("kubectl",),
        command_runner: CommandRunner | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        self._command = tuple(command)
        self._command_runner = command_runner or self._subprocess_run
        self._timeout_seconds = timeout_seconds

    def delete_expired(self, now: datetime | None = None) -> CleanupResult:
        current_time = now or datetime.now(UTC)
        namespaces = self._namespace_documents()
        deleted: list[str] = []
        for namespace in namespaces:
            metadata = namespace.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            name = str(metadata.get("name", ""))
            annotations = metadata.get("annotations", {})
            labels = metadata.get("labels", {})
            if not isinstance(annotations, dict) or not isinstance(labels, dict):
                continue
            if labels.get("kubecouncil.io/rehearsal") != "true":
                continue
            if not name.startswith("kc-rehearsal-"):
                continue
            expires_at = _parse_timestamp(str(annotations.get("kubecouncil.io/expires-at", "")))
            if expires_at is None or expires_at > current_time:
                continue
            self._run(("delete", "namespace", name, "--ignore-not-found=true"))
            deleted.append(name)
        return CleanupResult(inspected=len(namespaces), deleted=tuple(deleted))

    def _namespace_documents(self) -> list[dict[str, object]]:
        output = self._run(
            (
                "get",
                "namespaces",
                "-l",
                "kubecouncil.io/rehearsal=true",
                "-o",
                "json",
            )
        )
        document = json.loads(output) if output.strip() else {"items": []}
        items = document.get("items", []) if isinstance(document, dict) else []
        if not isinstance(items, list):
            raise CleanupError("kubectl returned malformed namespace list")
        return [item for item in items if isinstance(item, dict)]

    def _run(self, arguments: Sequence[str]) -> str:
        result = self._command_runner((*self._command, *arguments))
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "kubectl command failed"
            raise CleanupError(message)
        return result.stdout

    def _subprocess_run(self, arguments: Sequence[str]) -> CommandResult:
        try:
            result = subprocess.run(
                list(arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                returncode=124,
                stdout="",
                stderr=f"kubectl command timed out after {self._timeout_seconds}s",
            )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main() -> None:
    result = RehearsalNamespaceCleaner().delete_expired()
    print(json.dumps({"inspected": result.inspected, "deleted": list(result.deleted)}))


if __name__ == "__main__":
    main()
