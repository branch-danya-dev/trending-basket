"""Windows logon task with failure restart; no platform changes without confirmation."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import typer

from trending_basket.execution.ledger import digest

service_app = typer.Typer(help="Manage the local Demo process startup.")


def task_name(project: Path) -> str:
    return "TrendingBasketDemo-" + digest(str(project.resolve()))[:10]


def install_command(project: Path, xml_path: Path) -> list[str]:
    return ["schtasks", "/Create", "/TN", task_name(project), "/XML", str(xml_path), "/F"]


def task_xml(project: Path, launcher: Path, user: str) -> str:
    arguments = f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{launcher}"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
    <Enabled>true</Enabled>
    <UserId>{escape(user)}</UserId>
    </LogonTrigger>
    </Triggers>
  <Principals>
    <Principal id="Owner">
    <UserId>{escape(user)}</UserId>
    <LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
    </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
    <Interval>PT1M</Interval>
    <Count>999</Count>
    </RestartOnFailure>
    </Settings>
  <Actions Context="Owner">
    <Exec>
    <Command>powershell.exe</Command>
    <Arguments>{escape(arguments)}</Arguments>
    <WorkingDirectory>{escape(str(project))}</WorkingDirectory>
    </Exec>
    </Actions>
</Task>
"""


def launcher_text(project: Path, uv: str) -> str:
    def quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"Set-Location -LiteralPath {quoted(str(project))}\n"
        "$demoLog = Join-Path 'data/live/demo/logs' "
        "('run-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')\n"
        # Windows PowerShell 5 treats redirected native stderr as ErrorRecord;
        # ordinary uv build messages must not abort startup under Stop preference.
        f"$demoProcess = Start-Process -FilePath {quoted(uv)} "
        "-ArgumentList 'run','tb','run','--loop' -WindowStyle Hidden -Wait -PassThru "
        "-RedirectStandardOutput $demoLog -RedirectStandardError ($demoLog + '.stderr')\n"
        "exit $demoProcess.ExitCode\n"
    )


@service_app.command("install-windows")
def install_windows(dry_run: bool = False) -> None:
    if sys.platform != "win32" and not dry_run:
        raise typer.BadParameter("Windows Task Scheduler is required")
    project = Path.cwd().resolve()
    uv = shutil.which("uv")
    if not uv:
        raise typer.BadParameter("uv is not on PATH")
    directory = project / "data/live/demo/logs"
    launcher, xml_path = directory / "launch.ps1", directory / "task.xml"
    user = f"{os.environ.get('USERDOMAIN', '.')}\\{getpass.getuser()}"
    typer.echo(
        f"Task: {task_name(project)}\nAt user logon: uv run tb run --loop\n"
        f"Directory: {project}\nLogs: {directory}\n"
        "Restart on failure: every minute; no immediate launch."
    )
    if dry_run:
        typer.echo(task_xml(project, launcher, user))
        return
    typer.confirm("Create or replace this Windows task?", abort=True)
    directory.mkdir(parents=True, exist_ok=True)
    launcher.write_text(launcher_text(project, uv), encoding="utf-8-sig")
    xml_path.write_text(task_xml(project, launcher, user), encoding="utf-16")
    subprocess.run(install_command(project, xml_path), check=True)


@service_app.command("uninstall-windows")
def uninstall_windows() -> None:
    if sys.platform != "win32":
        raise typer.BadParameter("Windows Task Scheduler is required")
    name = task_name(Path.cwd())
    typer.confirm(f"Delete task {name}? This does not close positions.", abort=True)
    subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], check=True)
