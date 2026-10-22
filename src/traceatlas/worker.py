"""Isolated worker. Only writes the current attempt's staging directory."""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path


def main(spec_path: str | None = None) -> int:
    from .contracts import utc_now
    from .jobs import execute_job
    from .registry import ModuleRegistry

    spec = json.loads(Path(spec_path or sys.argv[1]).read_text(encoding="utf-8"))
    staging = Path(spec["staging_path"])
    cancel = threading.Event()
    output_lock = threading.Lock()
    sequence = 0

    def send(kind: str, **values):
        nonlocal sequence
        with output_lock:
            sequence += 1
            event = dict(protocol_version=1, kind=kind, task_id=spec["task_id"],
                         attempt_id=spec["attempt_id"], sequence=sequence,
                         timestamp=utc_now(), **values)
            print(json.dumps(event, ensure_ascii=True, default=str), flush=True)

    def commands():
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if command.get("attempt_id") == spec["attempt_id"] and command.get("command") == "cancel":
                    cancel.set()
            except (ValueError, TypeError):
                continue
        # A vanished owner must not leave a detached computation writing indefinitely.
        cancel.set()

    def heartbeat():
        while not finished.wait(2):
            send("heartbeat")

    finished = threading.Event()
    threading.Thread(target=commands, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        send("started", pid=os.getpid())
        ModuleRegistry().validate_task(spec)
        manifest = execute_job(spec, staging,
                               lambda stage, completed=None, total=None, message="": send(
                                   "progress", stage=stage, completed=completed, total=total, message=message),
                               cancel.is_set)
        if cancel.is_set():
            send("cancelled")
            return 2
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, default=str), encoding="utf-8")
        send("ready", manifest="manifest.json")
        return 0
    except Exception as exc:  # noqa: BLE001 - isolate any job failure from the desktop process
        code = "MEMORY_LIMIT" if isinstance(exc, MemoryError) else getattr(exc, "code", "WORKER_ERROR")
        if code == "CANCELLED":
            send("cancelled")
            return 2
        # Diagnostic trace deliberately excludes locals and business values.
        frames = [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                  for f in traceback.extract_tb(exc.__traceback__)]
        send("error", error={"code": code, "stage": getattr(exc, "stage", "execution"),
                             "message": str(exc) or "工作进程内存不足，任务已停止。",
                             "retryable": isinstance(exc, MemoryError) or getattr(exc, "retryable", False),
                             "details": getattr(exc, "details", {}), "diagnostic_id": spec["attempt_id"],
                             "frames": frames})
        return 1
    finally:
        finished.set()


if __name__ == "__main__":
    raise SystemExit(main())
