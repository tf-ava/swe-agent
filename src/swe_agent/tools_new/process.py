"""运行本机进程，收集退出码和输出。"""

import asyncio
import os
import signal
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path


_CLEANUP_TIMEOUT = 5


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    # 普通命令可能还启动了子进程，超时时一并终止。
    if os.name == "nt" and process.returncode is None:
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        try:
            cleanup = await asyncio.create_subprocess_exec(
                str(taskkill), "/PID", str(process.pid), "/T", "/F",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                await asyncio.wait_for(cleanup.wait(), _CLEANUP_TIMEOUT)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    cleanup.kill()
                await cleanup.wait()
        except OSError:
            pass
    elif os.name == "posix":
        # 这段代码用于 Linux、macOS 等 POSIX 系统。
        # 命令超时或被取消时，把它和它启动的程序一起停掉，免得测试还在后台跑。
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()


async def run_process(
    command: str | Sequence[str], *, cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    """字符串交给 shell，参数序列直接启动进程。"""
    # 在 Windows 上显式传递环境，保留值为空的变量。
    options = {
        "cwd": cwd,
        "env": os.environ.copy(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "start_new_session": os.name == "posix",
    }
    if isinstance(command, str):
        process = await asyncio.create_subprocess_shell(command, **options)
    else:
        process = await asyncio.create_subprocess_exec(*command, **options)

    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout)
    except (TimeoutError, asyncio.CancelledError):
        await _stop_process(process)
        with suppress(TimeoutError):
            await asyncio.wait_for(output, _CLEANUP_TIMEOUT)
        raise

    return subprocess.CompletedProcess(
        command, process.returncode,
        stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"),
    )
