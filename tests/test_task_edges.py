"""Failure-boundary tests using real pipes, subprocesses and Windows kernel jobs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import openpyxl
import psutil
import pytest

from traceatlas.project import Project
from traceatlas.tasks import TaskManager
from traceatlas.winjob import WorkerJob, is_memory_limit_exit


def sample(path, count=100):
    book = openpyxl.Workbook(write_only=True)
    sheet = book.create_sheet('流水')
    sheet.append(['账号', '金额'])
    for number in range(count):
        sheet.append([f'{number % 17:05d}', '1.123456789012'])
    book.save(path)
    return {'path': str(path), 'sheet': '流水', 'header_row': 1,
            'mapping': {'owner_account': 1, 'amount': 2}, 'platform': 'test', 'account_namespace': 'account'}


def eventually(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError('condition did not become true before timeout')


def terminal(manager, task_id, timeout=25):
    return eventually(lambda: (task if (task := manager.project.get_task(task_id))['state']
                               not in {'queued', 'running', 'cancel_requested', 'committing'} else None), timeout)


@pytest.fixture
def running_project(tmp_path):
    project = Project.create(tmp_path / 'project', 'edges')
    manager = TaskManager(project)
    yield project, manager
    manager.shutdown(cancel=True)
    project.close()


def substitute_worker(monkeypatch, script):
    """Exercise the actual scheduler with an adversarial executable protocol peer."""
    original = subprocess.Popen
    prelude = '''import json, sys, time
from pathlib import Path
spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def event(kind, sequence, **extra):
    value = dict(protocol_version=1, kind=kind, sequence=sequence,
                 task_id=spec['task_id'], attempt_id=spec['attempt_id'])
    value.update(extra)
    print(json.dumps(value), flush=True)
def ready(sequence=3):
    (Path(spec['staging_path']) / 'manifest.json').write_text(json.dumps({'files': [], 'objects': [], 'summary': {}}))
    event('ready', sequence)
'''
    def launch(command, **kwargs):
        return original([sys.executable, '-c', prelude + script, str(command[-1])], **kwargs)
    monkeypatch.setattr('traceatlas.tasks.subprocess.Popen', launch)


def test_running_cancel_stops_real_import_without_publishing(running_project, tmp_path):
    project, manager = running_project
    params = sample(tmp_path / 'long.xlsx', 40000)
    task_id = manager.submit('xlsx.import', params)
    eventually(lambda: project.get_task(task_id).get('progress', {}).get('completed', 0) > 0)
    pid = project.get_task(task_id)['worker_pid']
    started = time.monotonic()
    manager.cancel(task_id)
    task = terminal(manager, task_id)
    assert task['state'] == 'cancelled', task
    assert time.monotonic() - started < 12
    assert not psutil.pid_exists(pid)
    assert project.objects() == []


def test_cancel_during_commit_finishes_single_saved_result(running_project, tmp_path, monkeypatch):
    project, manager = running_project
    entered, release = threading.Event(), threading.Event()
    original = project.commit
    def gated_commit(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(project, 'commit', gated_commit)
    task_id = manager.submit('xlsx.import', sample(tmp_path / 'small.xlsx'))
    try:
        assert entered.wait(15)
        manager.cancel(task_id)
        assert project.get_task(task_id)['state'] == 'committing'
        assert any(event.get('kind') == 'notice' for event in manager.poll())
    finally:
        release.set()
    task = terminal(manager, task_id)
    assert task['state'] == 'succeeded', task
    assert len(project.objects('dataset')) == 1


def test_stale_attempt_wrong_task_and_reversed_sequence_are_ignored(running_project, tmp_path, monkeypatch):
    _project, manager = running_project
    substitute_worker(monkeypatch, '''
event('progress', 1000, attempt_id='previous-attempt', stage='bad-attempt')
event('progress', 1001, task_id='different-task', stage='bad-task')
event('progress', 2, stage='correct-stage', completed=7, total=10, message='valid')
event('progress', 1, stage='reversed-stage', completed=999, total=1000, message='invalid')
time.sleep(0.25)
ready(3)
''')
    task_id = manager.submit('xlsx.import', sample(tmp_path / 'small.xlsx', 1))
    task = terminal(manager, task_id)
    assert task['state'] == 'succeeded', task
    assert task['progress']['stage'] == 'correct-stage'
    assert task['progress']['completed'] == 7
    stages = {event.get('stage') for event in manager.poll()}
    assert not stages.intersection({'bad-attempt', 'bad-task', 'reversed-stage'})


@pytest.mark.parametrize('bad_message', ['not-json', '[]', '{"protocol_version":999,"kind":"ready"}'])
def test_malformed_protocol_is_failed_and_never_published(running_project, tmp_path, monkeypatch, bad_message):
    project, manager = running_project
    substitute_worker(monkeypatch, f'print({bad_message!r}, flush=True)\ntime.sleep(2)\nready()\n')
    task_id = manager.submit('xlsx.import', sample(tmp_path / 'small.xlsx', 1))
    task = terminal(manager, task_id)
    assert task['state'] == 'failed', task
    assert task['error']['code'] == 'PROTOCOL_ERROR'
    assert project.objects() == []
    assert not psutil.pid_exists(task['worker_pid'])


def test_bad_message_from_already_exited_worker_still_prevents_commit(running_project, tmp_path, monkeypatch):
    project, manager = running_project
    substitute_worker(monkeypatch, "ready()\nprint('not-json', flush=True)\n")
    original_update = project.update_task
    def slow_pid_registration(task_id, *args, **kwargs):
        result = original_update(task_id, *args, **kwargs)
        if 'worker_pid' in kwargs:
            # Model a slow database/storage write while the tiny worker exits.
            time.sleep(0.4)
        return result
    monkeypatch.setattr(project, 'update_task', slow_pid_registration)
    task_id = manager.submit('xlsx.import', sample(tmp_path / 'small.xlsx', 1))
    task = terminal(manager, task_id)
    assert task['state'] == 'failed', task
    assert task['error']['code'] == 'PROTOCOL_ERROR'
    assert project.objects() == []


def test_shutdown_cancels_running_worker_and_waits_for_process_exit(running_project, tmp_path):
    project, manager = running_project
    task_id = manager.submit('xlsx.import', sample(tmp_path / 'long.xlsx', 20000))
    eventually(lambda: project.get_task(task_id).get('worker_pid'))
    pid = project.get_task(task_id)['worker_pid']
    manager.shutdown(cancel=True)
    assert project.get_task(task_id)['state'] == 'cancelled'
    assert not psutil.pid_exists(pid)
    assert project.objects() == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel job contract')
def test_windows_job_close_kills_worker_and_descendant(tmp_path):
    marker = tmp_path / 'child.pid'
    code = '''import subprocess,sys,time
from pathlib import Path
sys.stdin.readline()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(marker)], stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    job = WorkerJob(process)
    try:
        process.stdin.write('start\n')
        process.stdin.flush()
        child_pid = int(eventually(lambda: marker.read_text() if marker.exists() else None))
        job.close()
        process.wait(timeout=10)
        eventually(lambda: not psutil.pid_exists(child_pid))
        assert not psutil.pid_exists(process.pid)
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdin.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel committed-memory budget')
def test_windows_job_rejects_allocation_above_low_test_budget():
    code = '''import sys
sys.stdin.readline()
try:
    allocated = bytearray(128 * 1024 * 1024)
    print('allocation-unexpectedly-succeeded', flush=True)
except MemoryError:
    print('memory-budget-enforced', flush=True)
'''
    process = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    job = WorkerJob(process, memory_bytes=64 * 1024**2)
    try:
        stdout, stderr = process.communicate('allocate\n', timeout=10)
        assert process.returncode == 0, stderr
        assert stdout.strip() == 'memory-budget-enforced'
        notices = []
        def saw_memory_notification():
            notices.extend(job.poll_notifications())
            return any(notice['name'] == 'job_memory_limit' for notice in notices)
        eventually(saw_memory_notification, timeout=3)
        # A Windows virtualenv launcher can run Python in a child process; the
        # limit applies to that whole process tree, not just the launcher PID.
        member_pids = {notice['pid'] for notice in notices if notice['name'] == 'new_process'}
        assert any(notice['name'] == 'job_memory_limit' and notice['pid'] in member_pids for notice in notices)
        assert job.memory_limit_exceeded
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert job.memory_limit_exceeded
    assert job.handle is None and job.completion_port is None
    assert job.poll_notifications() == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel committed-memory budget')
def test_windows_job_normal_exit_does_not_report_memory_limit():
    code = "import sys; sys.stdin.readline(); allocation = bytearray(1024 * 1024); print(len(allocation))"
    process = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    job = WorkerJob(process, memory_bytes=64 * 1024**2)
    try:
        stdout, stderr = process.communicate('allocate\n', timeout=10)
        assert process.returncode == 0, stderr
        assert stdout.strip() == str(1024 * 1024)
        assert not job.memory_limit_exceeded
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert not job.memory_limit_exceeded
    assert job.handle is None and job.completion_port is None


@pytest.mark.parametrize('status', [0xC0000007, 0xC0000017, 0xC000012D])
def test_known_memory_ntstatus_accepts_signed_and_unsigned_exit_codes(status):
    assert is_memory_limit_exit(status)
    assert is_memory_limit_exit(status - (1 << 32))


@pytest.mark.parametrize('status', [None, 0, 1, 8, -1, 0xC0000005, 0xC000013A])
def test_other_worker_exits_are_not_misreported_as_memory_limit(status):
    assert not is_memory_limit_exit(status)


def gate_worker_after_job_assignment(project, monkeypatch, gate):
    """Release an adversarial worker only after the scheduler assigned its job."""
    original_update = project.update_task
    def registered(task_id, *args, **kwargs):
        result = original_update(task_id, *args, **kwargs)
        if kwargs.get('worker_pid'):
            gate.touch()
        return result
    monkeypatch.setattr(project, 'update_task', registered)
    return f"while not Path({str(gate)!r}).exists(): time.sleep(.01)\n"


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel job failure classification')
def test_scheduler_classifies_real_job_memory_failure_without_worker_error_event(
        running_project, tmp_path, monkeypatch):
    project, manager = running_project
    budget = 128 * 1024**2
    original_create = project.create_task
    def create_with_test_budget(spec):
        spec = dict(spec, resource_limits=dict(spec['resource_limits'], memory_bytes=budget))
        return original_create(spec)
    monkeypatch.setattr(project, 'create_task', create_with_test_budget)
    gate = gate_worker_after_job_assignment(project, monkeypatch, tmp_path / 'memory-job-assigned')
    substitute_worker(monkeypatch, gate + '''
try:
    allocation = bytearray(256 * 1024 * 1024)
except MemoryError:
    # No protocol error or explicit memory error is sent. Exit code 1 alone
    # cannot identify the cause: the scheduler must use the real job notice.
    sys.exit(1)
ready()
''')
    task_id = manager.submit('xlsx.import', sample(tmp_path / 'memory.xlsx', 1))
    task = terminal(manager, task_id)
    assert task['resource_limits']['memory_bytes'] == budget
    assert task['worker_exit_code'] == 1
    assert task['state'] == 'failed', task
    assert task['error']['code'] == 'MEMORY_LIMIT'
    assert task['error']['retryable'] is True
    assert task['error']['stage'] == 'execution'
    assert project.objects() == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows native exit status classification')
@pytest.mark.parametrize('status', [0xC0000007, 0xC0000017, 0xC000012D])
def test_scheduler_classifies_native_memory_exit_without_job_budget_breach(
        running_project, tmp_path, monkeypatch, status):
    project, manager = running_project
    gate = gate_worker_after_job_assignment(project, monkeypatch, tmp_path / 'native-job-assigned')
    substitute_worker(monkeypatch, gate + f'import ctypes\nctypes.windll.kernel32.ExitProcess({status})\n')
    task_id = manager.submit('xlsx.import', sample(tmp_path / 'native.xlsx', 1))
    task = terminal(manager, task_id)
    assert task['worker_exit_code'] & 0xFFFFFFFF == status
    assert task['state'] == 'failed', task
    assert task['error']['code'] == 'MEMORY_LIMIT'
    assert task['error']['retryable'] is True
    assert project.objects() == []


def test_scheduler_keeps_ordinary_forced_worker_exit_interrupted(running_project, tmp_path, monkeypatch):
    project, manager = running_project
    gate = gate_worker_after_job_assignment(project, monkeypatch, tmp_path / 'kill-job-assigned')
    substitute_worker(monkeypatch, gate + "event('progress', 1, stage='waiting')\ntime.sleep(60)\n")
    task_id = manager.submit('xlsx.import', sample(tmp_path / 'killed.xlsx', 1))
    eventually(lambda: project.get_task(task_id).get('progress', {}).get('stage') == 'waiting')
    process = psutil.Process(project.get_task(task_id)['worker_pid'])
    for child in process.children(recursive=True):
        child.kill()
    try:
        process.kill()
    except psutil.NoSuchProcess:
        pass
    task = terminal(manager, task_id)
    assert task['state'] == 'interrupted', task
    assert task['error']['code'] == 'WORKER_EXIT'
    assert task['error']['retryable'] is True
    assert project.objects() == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows process owner termination')
def test_killing_real_app_owner_terminates_its_worker(tmp_path):
    params = sample(tmp_path / 'long.xlsx', 40000)
    marker, project_path = tmp_path / 'worker.json', tmp_path / 'owned-project'
    parent_script = '''import json,sys,time
from pathlib import Path
from traceatlas.project import Project
from traceatlas.tasks import TaskManager
project = Project.create(sys.argv[1], 'owner-death')
manager = TaskManager(project)
task_id = manager.submit('xlsx.import', json.loads(sys.argv[3]))
while True:
    task = project.get_task(task_id)
    if task.get('worker_pid') and task.get('worker_created_at'):
        Path(sys.argv[2]).write_text(json.dumps({'pid':task['worker_pid'], 'task_id':task_id,
                                              'created':task['worker_created_at']}))
        break
    time.sleep(.01)
time.sleep(60)
'''
    owner = subprocess.Popen([sys.executable, '-c', parent_script, str(project_path), str(marker), json.dumps(params)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = None
    try:
        details = json.loads(eventually(lambda: marker.read_text() if marker.exists() else None))
        worker = psutil.Process(details['pid'])
        assert worker.is_running()
        owner.kill()
        owner.wait(timeout=10)
        eventually(lambda: not psutil.pid_exists(details['pid']))
        with Project.open(project_path) as recovered:
            assert recovered.get_task(details['task_id'])['state'] == 'interrupted'
            assert recovered.objects() == []
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=10)
        if worker and worker.is_running():
            worker.kill()


def test_recovery_protects_attempt_when_previous_worker_still_alive(tmp_path):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        with Project.create(tmp_path / 'project', 'worker protection') as project:
            task = project.create_task({'feature_id': 'xlsx.import', 'parameters': {}})
            project.update_task(task['task_id'], 'running', worker_pid=process.pid,
                                worker_created_at=psutil.Process(process.pid).create_time())
            staging = project.path / 'staging' / task['attempt_id']
            staging.mkdir()
            (staging / 'partial').write_bytes(b'active')
        with Project.open(tmp_path / 'project') as recovered:
            assert recovered.get_task(task['task_id'])['state'] == 'running'
            assert recovered.cleanup_staging()['removed'] == []
            assert staging.exists()
            assert any('工作进程仍在' in message for message in recovered.recovery_report)
        process.kill()
        process.wait(timeout=10)
        with Project.open(tmp_path / 'project') as recovered:
            assert recovered.get_task(task['task_id'])['state'] == 'interrupted'
            assert recovered.cleanup_staging()['removed'] == [task['attempt_id']]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def test_recovery_does_not_confuse_reused_pid_with_original_worker(tmp_path):
    with Project.create(tmp_path / 'project', 'pid reuse') as project:
        task = project.create_task({'feature_id': 'xlsx.import', 'parameters': {}})
        project.update_task(task['task_id'], 'running', worker_pid=os.getpid(),
                            worker_created_at=psutil.Process().create_time() - 10000)
    with Project.open(tmp_path / 'project') as recovered:
        assert recovered.get_task(task['task_id'])['state'] == 'interrupted'
