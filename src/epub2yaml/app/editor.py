from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EditorLaunchResult:
    command: str
    exit_code: int | None
    waited: bool
    error: str | None = None


class EditorLauncher:
    def resolve_command(self, file_path: Path) -> str:
        template = self._resolve_editor_template()
        normalized_path = str(file_path)
        if "{file}" in template:
            return template.format(file=normalized_path)
        return f'{template} "{normalized_path}"'

    def open(self, file_path: Path) -> EditorLaunchResult:
        command = self.resolve_command(file_path)
        try:
            completed = subprocess.run(
                command,
                check=False,
                shell=True,
                cwd=str(file_path.parent),
            )
        except OSError as exc:
            return EditorLaunchResult(command=command, exit_code=None, waited=False, error=str(exc))
        return EditorLaunchResult(command=command, exit_code=completed.returncode, waited=True)

    def _resolve_editor_template(self) -> str:
        for env_name in ("EPUB2YAML_EDITOR", "VISUAL", "EDITOR"):
            value = os.environ.get(env_name, "").strip()
            if value:
                return value

        if sys.platform.startswith("win"):
            return "notepad {file}"
        if sys.platform == "darwin":
            return "open -W -a TextEdit {file}"
        return "nano {file}"


def split_command_preview(command: str) -> list[str]:
    return shlex.split(command, posix=not sys.platform.startswith("win"))
