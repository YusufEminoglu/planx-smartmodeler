"""Bounded local workspace access for the Developer Agent scope.

The workspace is always one resolved plugin directory. There is no shell
interpreter here: read/search operations use pathlib and command execution is
an explicit allowlist of diagnostic commands. File writes are performed only
after a validated ``WorkspacePatchProposal`` reaches the trusted apply
boundary.
"""
from __future__ import annotations

import hashlib
import difflib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence, Tuple

from .proposals import WorkspacePatchOperation, WorkspacePatchProposal

MAX_READ_CHARS = 120_000
MAX_LIST_ITEMS = 200
MAX_SEARCH_MATCHES = 100
MAX_COMMAND_OUTPUT_CHARS = 20_000
MAX_COMMAND_SECONDS = 120
MAX_PREVIEW_DIFF_CHARS = 50_000

_IGNORED_NAMES = {".git", ".venv", "__pycache__", ".pytest_cache", ".idea"}
_GIT_EXECUTABLE = shutil.which("git") or "C:/Program Files/Git/cmd/git.exe"
_QGIS_PYTHON = sys.executable
if _QGIS_PYTHON.casefold().endswith((".bat", ".cmd")):
    _PYTHON_LAUNCH = (
        "C:/Windows/System32/cmd.exe",
        "/d",
        "/c",
        _QGIS_PYTHON,
    )
else:
    _PYTHON_LAUNCH = (_QGIS_PYTHON,)
_COMMANDS = {
    "git_status": (_GIT_EXECUTABLE, "status", "--short"),
    "git_diff": (_GIT_EXECUTABLE, "diff", "--stat"),
    "pytest": (*_PYTHON_LAUNCH, "-m", "pytest", "-q", "-p", "no:cacheprovider"),
}


class WorkspaceError(ValueError):
    """A safe, user-facing workspace boundary error."""


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class WorkspaceManager:
    """Operate only below one resolved plugin root."""

    def __init__(self, root: os.PathLike[str] | str, workspace_id: str = "planx_smartmodeler") -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise WorkspaceError("The configured workspace root is unavailable.")
        self.workspace_id = workspace_id

    def _relative(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise WorkspaceError("Workspace path must be a non-empty relative path.")
        normalized = value.replace("\\", "/").strip()
        candidate = (self.root / normalized).resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError("Workspace path escapes the plugin root.") from error
        parts = relative.parts
        if not parts or any(part in _IGNORED_NAMES for part in parts):
            raise WorkspaceError("That workspace path is outside the supported source area.")
        return "/".join(parts)

    def _path(self, value: Any, *, must_exist: bool = False) -> Tuple[str, Path]:
        relative = self._relative(value)
        path = (self.root / relative).resolve()
        if must_exist and not path.is_file():
            raise WorkspaceError("The requested workspace file does not exist.")
        return relative, path

    def file_state(self, relative: str) -> Dict[str, Any]:
        clean, path = self._path(relative)
        if not path.exists():
            return {"path": clean, "exists": False, "digest": "missing"}
        if not path.is_file():
            raise WorkspaceError("Workspace state can describe files only.")
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"path": clean, "exists": True, "digest": _digest_text(text)}

    def state(self, paths: Iterable[str]) -> Dict[str, Any]:
        items = [self.file_state(path) for path in paths]
        items.sort(key=lambda item: item["path"])
        return {"workspace_id": self.workspace_id, "files": items}

    def list(self, relative: str = "") -> Dict[str, Any]:
        if relative.strip():
            clean = self._relative(relative)
            directory = (self.root / clean).resolve()
        else:
            clean = ""
            directory = self.root
        if not directory.is_dir():
            raise WorkspaceError("The requested workspace directory does not exist.")
        entries = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if child.name in _IGNORED_NAMES:
                continue
            child_relative = "/".join(part for part in (clean, child.name) if part)
            entries.append(
                {
                    "path": child_relative,
                    "kind": "directory" if child.is_dir() else "file",
                }
            )
            if len(entries) >= MAX_LIST_ITEMS:
                break
        return {"workspace_id": self.workspace_id, "path": clean, "entries": entries}

    def read(self, relative: str, max_chars: int = MAX_READ_CHARS) -> Dict[str, Any]:
        clean, path = self._path(relative, must_exist=True)
        if path.stat().st_size > MAX_READ_CHARS * 4:
            raise WorkspaceError("The requested workspace file is too large to inspect.")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceError("The requested workspace file is not UTF-8 text.") from error
        limit = max(1, min(int(max_chars), MAX_READ_CHARS))
        return {
            "workspace_id": self.workspace_id,
            "path": clean,
            "content": text[:limit],
            "truncated": len(text) > limit,
            "digest": _digest_text(text),
            "context_state": self.state((clean,)),
        }

    def search(self, query: str, relative: str = "") -> Dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise WorkspaceError("Search text must be between 1 and 200 characters.")
        root = self.root if not relative.strip() else (self.root / self._relative(relative)).resolve()
        if not root.is_dir():
            raise WorkspaceError("The requested search directory does not exist.")
        matches = []
        for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
            if not path.is_file() or any(part in _IGNORED_NAMES for part in path.relative_to(self.root).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append(
                        {
                            "path": str(path.relative_to(self.root)).replace("\\", "/"),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        return {"workspace_id": self.workspace_id, "matches": matches, "truncated": True}
        return {"workspace_id": self.workspace_id, "matches": matches, "truncated": False}

    def command(self, command_id: str) -> Dict[str, Any]:
        if command_id not in _COMMANDS:
            raise WorkspaceError("That workspace command is not on the safe allowlist.")
        try:
            from qgis.PyQt.QtCore import QProcess
        except ImportError as error:
            raise WorkspaceError("Workspace diagnostics require the QGIS runtime.") from error
        command = _COMMANDS[command_id]
        process = QProcess()
        process.setWorkingDirectory(str(self.root))
        process.setProgram(command[0])
        process.setArguments(list(command[1:]))
        process.start()
        if not process.waitForStarted(5_000):
            raise WorkspaceError("The workspace diagnostic could not be started.")
        if not process.waitForFinished(MAX_COMMAND_SECONDS * 1_000):
            process.kill()
            process.waitForFinished(2_000)
            raise WorkspaceError("The workspace diagnostic timed out.")
        stdout = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        stderr = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        output = stdout + stderr
        return {
            "workspace_id": self.workspace_id,
            "command": command_id,
            "return_code": int(process.exitCode()),
            "output": output[:MAX_COMMAND_OUTPUT_CHARS],
            "truncated": len(output) > MAX_COMMAND_OUTPUT_CHARS,
        }

    def _resolve_patch(
        self, proposal: WorkspacePatchProposal
    ) -> list[Tuple[WorkspacePatchOperation, str, Path]]:
        if proposal.workspace_id != self.workspace_id:
            raise WorkspaceError("The patch targets a different workspace.")
        resolved: list[Tuple[WorkspacePatchOperation, str, Path]] = []
        for operation in proposal.operations:
            clean, path = self._path(operation.path)
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != operation.old_text:
                raise WorkspaceError(f"Workspace file changed before apply: {clean}.")
            if not path.exists() and operation.old_text:
                raise WorkspaceError(f"Workspace create operation has a non-empty base: {clean}.")
            resolved.append((operation, clean, path))
        return resolved

    def preview(self, proposal: WorkspacePatchProposal) -> Dict[str, Any]:
        """Validate exact old text and return a bounded human-readable diff."""
        resolved = self._resolve_patch(proposal)
        chunks = []
        changes = []
        for operation, clean, _path in resolved:
            diff = difflib.unified_diff(
                operation.old_text.splitlines(keepends=True),
                operation.new_text.splitlines(keepends=True),
                fromfile=f"a/{clean}",
                tofile=f"b/{clean}",
            )
            chunks.extend(diff)
            changes.append(
                {
                    "path": clean,
                    "old_chars": len(operation.old_text),
                    "new_chars": len(operation.new_text),
                }
            )
        source = "".join(chunks)[:MAX_PREVIEW_DIFF_CHARS]
        return {
            "kind": proposal.kind,
            "title": proposal.title,
            "target": f"Workspace: {self.workspace_id}",
            "summary": proposal.summary,
            "warnings": list(proposal.warnings),
            "operations": changes,
            "changes": changes,
            "operation_count": len(changes),
            "source": source,
            "source_language": "unified diff",
            "applied": False,
            "truncated": len("".join(chunks)) > MAX_PREVIEW_DIFF_CHARS,
        }

    def apply(self, proposal: WorkspacePatchProposal) -> Tuple[Dict[str, Any], Tuple[Tuple[str, str, str], ...]]:
        resolved = self._resolve_patch(proposal)
        backups = tuple((clean, operation.old_text, operation.new_text) for operation, clean, _ in resolved)
        written: list[Path] = []
        try:
            for operation, _clean, path in resolved:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="", dir=str(path.parent), delete=False
                ) as temporary:
                    temporary.write(operation.new_text)
                    temporary_path = Path(temporary.name)
                os.replace(temporary_path, path)
                written.append(path)
        except Exception as error:
            for operation, _clean, path in resolved:
                if path in written:
                    path.write_text(operation.old_text, encoding="utf-8", newline="")
            raise WorkspaceError("The workspace patch was rolled back after a write failure.") from error
        return (
            {"workspace_id": self.workspace_id, "files": [clean for _op, clean, _path in resolved]},
            backups,
        )

    def undo(self, backups: Sequence[Tuple[str, str, str]]) -> None:
        for relative, old_text, new_text in backups:
            clean, path = self._path(relative, must_exist=True)
            current = path.read_text(encoding="utf-8")
            if current != new_text:
                raise WorkspaceError(f"Workspace file changed after apply: {clean}.")
        for relative, old_text, _new_text in backups:
            _clean, path = self._path(relative)
            path.write_text(old_text, encoding="utf-8", newline="")
