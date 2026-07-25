from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class NativeFolderPickerUnavailable(RuntimeError):
    pass


def _run(command: list[str], *, timeout: int = 300) -> str | None:
    try:
        kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "check": False,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        completed = subprocess.run(command, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeFolderPickerUnavailable(str(exc)) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if completed.returncode in {1, 130} and not stderr:
            return None
        raise NativeFolderPickerUnavailable(stderr or f"Folder picker exited with code {completed.returncode}.")
    value = completed.stdout.strip().splitlines()
    if not value:
        return None
    selected = Path(value[-1].strip()).expanduser()
    if not selected.is_dir():
        raise NativeFolderPickerUnavailable("The selected folder is no longer available.")
    return str(selected.resolve())


def pick_local_folder() -> str | None:
    """Open an operating-system folder chooser on the local owner machine.

    The environment override exists only to make the owner-only API testable in
    headless environments. It is also useful for scripted local deployments.
    """
    override = os.environ.get("FORGETRACE_TEST_PICK_FOLDER", "").strip()
    if override:
        selected = Path(override).expanduser()
        if not selected.is_dir():
            raise NativeFolderPickerUnavailable("FORGETRACE_TEST_PICK_FOLDER is not a directory.")
        return str(selected.resolve())

    if os.name == "nt":
        powershell = (
            shutil.which("pwsh.exe") or shutil.which("pwsh")
            or shutil.which("powershell.exe") or shutil.which("powershell")
        )
        if not powershell:
            raise NativeFolderPickerUnavailable("PowerShell 7 or Windows PowerShell is unavailable.")
        script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select the complete folder to import into ForgeTrace'
$dialog.ShowNewFolderButton = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  Write-Output $dialog.SelectedPath
}
"""
        return _run([powershell, "-NoProfile", "-STA", "-Command", script])

    if sys.platform == "darwin":
        osascript = shutil.which("osascript")
        if not osascript:
            raise NativeFolderPickerUnavailable("AppleScript is unavailable.")
        script = 'POSIX path of (choose folder with prompt "Select the complete folder to import into ForgeTrace")'
        return _run([osascript, "-e", script])

    zenity = shutil.which("zenity")
    if zenity:
        return _run([zenity, "--file-selection", "--directory", "--title=Select the complete folder to import into ForgeTrace"])
    kdialog = shutil.which("kdialog")
    if kdialog:
        return _run([kdialog, "--getexistingdirectory", str(Path.home()), "--title", "Select the complete folder to import into ForgeTrace"])
    raise NativeFolderPickerUnavailable("No supported native folder picker was found. Use the browser folder picker instead.")
