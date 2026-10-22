"""Durable, serial task scheduler independent of Qt and its event loop."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil

from .contracts import TraceAtlasError, new_id, utc_now
from .registry import ModuleRegistry
from .winjob import WorkerJob, is_memory_limit_exit

ACTIVE = {"queued", "running", "cancel_requested", "committing"}
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
FEATURES = {"xlsx.import", "validation.account_count", "result.export", "index.rebuild"}


class TaskManager:
    def __init__(self, project):
        self.project = project
        self._events: queue.Queue = queue.Queue()
        self._queue: deque[str] = deque()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._worker_job = None
        self._active_id = None
        self._cancel_at = None
        self._sequences = {}
        self.registry = ModuleRegistry()
        self._thread = threading.Thread(target=self._run, name="TraceAtlas scheduler", daemon=True)
        self._thread.start()

    def submit(self, feature_id, parameters, input_ids=None):
        if self.project.readonly:
            raise TraceAtlasError("项目以只读方式打开，不能运行新任务。", code="READ_ONLY")
        if feature_id not in FEATURES:
            raise TraceAtlasError("功能未注册或当前版本不支持。", code="FEATURE_UNAVAILABLE")
        params = self.registry.validate_parameters(feature_id, parameters)
        inputs = list(input_ids or params.get("input_ids", []))
        if feature_id == "result.export" and params.get("object_id"):
            inputs = [params["object_id"]]
        if feature_id in {"validation.account_count", "index.rebuild"} and not inputs:
            raise TraceAtlasError("请选择至少一个已提交的数据集。", code="INPUT_REQUIRED")
        for oid in inputs:
            obj = self.project.get_object(oid)
            if obj.get("state") not in (None, "available", "valid", "ready", "committed"):
                raise TraceAtlasError("输入已损坏、失效或删除，请先修复或选择其他版本。", code="INPUT_UNAVAILABLE")
            if feature_id in {"validation.account_count", "index.rebuild"} and obj["kind"] != "dataset":
                raise TraceAtlasError("该任务需要标准流水数据集。", code="INPUT_TYPE")
        if feature_id == "xlsx.import":
            if not Path(params.get("path", "")).is_file():
                raise TraceAtlasError("输入文件不存在，请重新选择。", code="SOURCE_MISSING")
            if not params.get("mapping"):
                raise TraceAtlasError("请先确认字段映射。", code="MAPPING_REQUIRED")
        tid = new_id()
        feature = self.registry.get_feature(feature_id)
        spec = {"task_id": tid, "project_id": self.project.id, "feature_id": feature_id,
                "module_version": feature["module_version"], "protocol_version": 1,
                "parameter_schema_version": feature["parameter_schema_version"],
                "input_dataset_versions": inputs, "selection": params.get("filters", []),
                "parameters": params, "resource_limits": {"memory_bytes": 4 * 1024**3,
                                                            "cancel_timeout_seconds": 10},
                "created_at": utc_now()}
        with self._lock:
            self.project.create_task(spec)
            self._queue.append(tid)
            self._wake.set()
        return tid

    def list_tasks(self):
        result = []
        for task in self.project.list_tasks():
            row = dict(task)
            row.update(task.get("progress") or {})
            row["id"] = row.get("task_id", row.get("id"))
            row["task_id"] = row["id"]
            row["status"] = row.get("state")
            row["attempt_id"] = row.get("current_attempt_id", row.get("attempt_id"))
            result.append(row)
        return result

    def has_active(self):
        return any(t.get("state") in ACTIVE for t in self.project.list_tasks())

    def poll(self):
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def cancel(self, task_id):
        with self._lock:
            task = self.project.get_task(task_id)
            state = task["state"]
            if state == "queued":
                self._queue = deque(t for t in self._queue if t != task_id)
                self._set_state(task_id, "cancelled")
            elif state == "running":
                self._set_state(task_id, "cancel_requested")
                self._cancel_at = time.monotonic()
                if self._process and self._process.stdin:
                    try:
                        self._process.stdin.write(json.dumps({"command": "cancel", "attempt_id":
                            task["current_attempt_id"]}) + "\n")
                        self._process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
            elif state == "committing":
                self._events.put({"kind": "notice", "task_id": task_id,
                                  "message": "正在完成安全保存，完成后可查看结果。"})

    def retry(self, task_id):
        with self._lock:
            task = self.project.get_task(task_id)
            if task["state"] not in {"failed", "interrupted", "cancelled"}:
                raise TraceAtlasError("仅失败、中断或取消的任务可以重试。", code="INVALID_TASK_STATE")
            if task.get("error") and task["error"].get("retryable") is False:
                raise TraceAtlasError("请先处理输入问题并重新提交任务。", code="NOT_RETRYABLE")
            self.registry.validate_task(task)
            self.project.create_attempt(task_id)
            self._queue.append(task_id)
            self._wake.set()
        return task_id

    def shutdown(self, cancel=False):
        if cancel:
            for task in self.project.list_tasks():
                if task["state"] in ACTIVE:
                    self.cancel(task["task_id"])
        if self.has_active() and not cancel:
            raise TraceAtlasError("仍有任务运行，请等待或取消后关闭。", code="TASKS_ACTIVE")
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            raise TraceAtlasError("任务仍在安全保存，请稍后再关闭。", code="TASKS_ACTIVE")

    def _set_state(self, tid, state, **fields):
        self.project.update_task(tid, state=state, **fields)
        self._events.put({"kind": "state", "task_id": tid, "state": state, **fields})

    def _run(self):
        while True:
            self._wake.wait(0.2)
            self._wake.clear()
            with self._lock:
                if self._stop.is_set() and not self._queue:
                    return
                tid = self._queue.popleft() if self._queue else None
            if tid:
                try:
                    self._execute(tid)
                except Exception as exc:  # noqa: BLE001 - persist failure at the scheduler boundary
                    if self._process and self._process.poll() is None:
                        self._terminate(self._process)
                        self._process.wait(timeout=10)
                    self._set_state(tid, "failed", error={
                        "code": "MEMORY_LIMIT" if isinstance(exc, MemoryError) else getattr(exc, "code", "SCHEDULER_ERROR"),
                        "message": str(exc) or "保存任务时内存不足，已保存的数据不受影响。",
                        "stage": getattr(exc, "stage", "scheduler"),
                        "retryable": isinstance(exc, MemoryError) or getattr(exc, "retryable", True),
                        "details": getattr(exc, "details", {})})
                finally:
                    with self._lock:
                        if self._worker_job:
                            self._worker_job.close()
                            self._worker_job = None
                        self._process = None
                        self._active_id = None
                        self._cancel_at = None

    def _execute(self, tid):
        task = self.project.get_task(tid)
        self.registry.validate_task(task)
        attempt_id = task["current_attempt_id"]
        staging = self.project.path / "staging" / attempt_id
        staging.mkdir(parents=True, exist_ok=True)
        spec = dict(task)
        spec.update(attempt_id=attempt_id, project_path=str(self.project.path), staging_path=str(staging),
                    inputs=[self.project.get_object(i) for i in task.get("input_dataset_versions", [])])
        spec_path = staging / "task.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, default=str), encoding="utf-8")
        if getattr(sys, "frozen", False):
            worker_exe = Path(sys.executable).with_name("TraceAtlasWorker.exe")
            command = [str(worker_exe), str(spec_path)]
        else:
            command = [sys.executable, "-m", "traceatlas.worker", str(spec_path)]
        event_queue: queue.Queue = queue.Queue()
        with self._lock:
            if self.project.get_task(tid)["state"] == "cancelled":
                return
            self._set_state(tid, "running", started_at=utc_now(), error=None)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            process = self._process
            self._worker_job = WorkerJob(process, task.get('resource_limits', {}).get('memory_bytes', 4 * 1024**3))
            self._active_id = tid
            self.project.update_task(tid, worker_pid=process.pid,
                                     worker_created_at=psutil.Process(process.pid).create_time())

        def read_events():
            for line in process.stdout:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get('protocol_version') != 1:
                        event_queue.put({'kind': 'protocol_error'})
                    else:
                        event_queue.put(event)
                except ValueError:
                    event_queue.put({"kind": "protocol_error"})
            event_queue.put({"kind": "eof"})

        def drain_stderr():
            # Never block a worker on a full stderr pipe; do not log business values.
            for _ in process.stderr:
                pass

        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        threading.Thread(target=drain_stderr, daemon=True).start()
        ready = False
        cancelled = False
        error = None
        last_update = 0.0
        peak = 0
        limits = task.get("resource_limits", {})
        while process.poll() is None or reader.is_alive() or not event_queue.empty():
            try:
                event = event_queue.get(timeout=0.1)
            except queue.Empty:
                event = {}
            if event.get("kind") == "protocol_error":
                error = {"code": "PROTOCOL_ERROR", "stage": "execution", "retryable": False,
                         "message": "工作进程返回了不受支持的消息，任务已停止。"}
                if process.poll() is None:
                    self._terminate(process)
            if event.get("task_id") == tid and event.get("attempt_id") == attempt_id:
                sequence = event.get("sequence", 0)
                if sequence > self._sequences.get(attempt_id, 0):
                    self._sequences[attempt_id] = sequence
                    kind = event.get("kind")
                    self._events.put(event)
                    if kind == "progress" and time.monotonic() - last_update >= 0.2:
                        self.project.update_task(tid, progress={k: event.get(k) for k in
                            ("stage", "completed", "total", "message")})
                        last_update = time.monotonic()
                    elif kind == "ready":
                        ready = True
                    elif kind == "cancelled":
                        cancelled = True
                    elif kind == "error":
                        error = event["error"]
            if process.poll() is None:
                try:
                    proc = psutil.Process(process.pid)
                    rss = proc.memory_info().rss + sum(p.memory_info().rss for p in proc.children(recursive=True)
                                                       if p.is_running())
                    peak = max(peak, rss)
                    if rss > limits.get("memory_bytes", 4 * 1024**3):
                        error = {"code": "MEMORY_LIMIT", "stage": "execution", "retryable": True,
                                 "message": "任务达到 4GB 内存限制，已停止；已保存的数据不受影响。"}
                        self._terminate(process)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                if self._cancel_at and time.monotonic() - self._cancel_at >= limits.get("cancel_timeout_seconds", 10):
                    self._terminate(process)
                    cancelled = True
        process.wait()
        if error is None and (self._worker_job.memory_limit_exceeded
                              or is_memory_limit_exit(process.returncode)):
            error = {"code": "MEMORY_LIMIT", "stage": "execution", "retryable": True,
                     "message": "工作进程内存不足或达到任务内存限制，已停止；已保存的数据不受影响。"}
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
        self.project.update_task(tid, peak_memory_bytes=peak, worker_exit_code=process.returncode)
        with self._lock:
            if self._cancel_at or cancelled:
                self._set_state(tid, "cancelled", finished_at=utc_now())
                return
            if error:
                self._set_state(tid, "failed", error=error, finished_at=utc_now())
                return
            if not ready or process.returncode != 0:
                self._set_state(tid, "interrupted", error={"code": "WORKER_EXIT", "stage": "execution",
                    "message": "工作进程意外退出，历史结果已保留，可以重试。", "retryable": True}, finished_at=utc_now())
                return
            self._set_state(tid, "committing")
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        self.project.commit(manifest, tid, attempt_id)
        self._events.put({"kind": "state", "task_id": tid, "state": "succeeded"})

    @staticmethod
    def _terminate(process):
        try:
            proc = psutil.Process(process.pid)
            for child in proc.children(recursive=True):
                child.kill()
            proc.kill()
        except psutil.NoSuchProcess:
            pass
