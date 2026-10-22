"""Measure real TaskManager jobs, optionally launching the built Windows worker.

Run from the repository root, after the bundle has finished building:
    .venv/Scripts/python.exe scripts/benchmark_scheduler.py --bundle dist/TraceAtlas

--source reuses a synthetic workbook from benchmark_data.generate_xlsx; --rows
must match that workbook. Queries and the scheduler still run in source Python.
This is neither a packaged GUI test nor clean-Windows acceptance.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import psutil
from benchmark_data import Measurement, generate_xlsx

from traceatlas.project import Project
from traceatlas.query import QueryService
from traceatlas.tasks import ACTIVE, TaskManager


@contextmanager
def worker_environment(bundle):
    """Exercise the existing frozen-worker selection with a sanitized child env."""
    if bundle is None:
        yield
        return
    bundle = bundle.resolve()
    executable, worker = bundle / 'TraceAtlas.exe', bundle / 'TraceAtlasWorker.exe'
    if not executable.is_file() or not worker.is_file():
        raise FileNotFoundError(f'Complete the bundle build first: {executable} and {worker} are required')
    original_executable = sys.executable
    had_frozen = hasattr(sys, 'frozen')
    original_frozen = getattr(sys, 'frozen', None)
    originals = {name: value for name, value in os.environ.items()
                 if name.upper().startswith('PYTHON') or name.upper() in {'PATH', 'VIRTUAL_ENV'}}
    try:
        for name in originals:
            os.environ.pop(name, None)
        windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        os.environ['PATH'] = os.pathsep.join(map(str, [windows / 'System32', windows, windows / 'System32/Wbem']))
        sys.executable = str(executable)
        sys.frozen = True
        yield
    finally:
        sys.executable = original_executable
        if had_frozen:
            sys.frozen = original_frozen
        else:
            del sys.frozen
        for name in list(os.environ):
            if name.upper().startswith('PYTHON') or name.upper() in {'PATH', 'VIRTUAL_ENV'}:
                os.environ.pop(name, None)
        os.environ.update(originals)


def expected_account_counts(rows):
    counts = {}
    for number in range(1, rows + 1):
        if number % 50003 == 0:
            continue
        value = number - 1 if number % 200007 == 0 else number
        account = f'{value % 10000:05d}'
        counts[account] = counts.get(account, 0) + 1
    return counts


def run_task(manager, measurement, task_metrics, label, feature, parameters, inputs, timeout):
    started = time.perf_counter()
    task_id = None
    try:
        with measurement.stage(label):
            task_id = manager.submit(feature, parameters, input_ids=inputs)
            deadline = time.monotonic() + timeout
            next_status = time.monotonic() + 30
            while True:
                task = manager.project.get_task(task_id)
                manager.poll()  # The benchmark has no UI consumer; drain progress events.
                if task['state'] not in ACTIVE:
                    break
                if time.monotonic() >= deadline:
                    manager.cancel(task_id)
                    raise TimeoutError(f'{label} exceeded {timeout:g} seconds; cancellation requested')
                if time.monotonic() >= next_status:
                    print(json.dumps({'task': label, 'state': task['state'],
                                      'progress': task.get('progress')}, ensure_ascii=False), flush=True)
                    next_status = time.monotonic() + 30
                time.sleep(.05)
            if task['state'] != 'succeeded':
                raise RuntimeError(f'{label}: {task["state"]}: {task.get("error")}')
        return task
    finally:
        task = manager.project.get_task(task_id) if task_id else {}
        metrics = {'stage': label, 'feature_id': feature, 'task_id': task_id,
                   'state': task.get('state', 'not_submitted'),
                   'elapsed_seconds': round(time.perf_counter() - started, 4),
                   'worker_tree_peak_rss_mib': round(task.get('peak_memory_bytes', 0) / 1024**2, 2),
                   'worker_exit_code': task.get('worker_exit_code'),
                   'job_committed_memory_limit_bytes': task.get('resource_limits', {}).get('memory_bytes'),
                   'error': task.get('error')}
        task_metrics.append(metrics)
        print(json.dumps({'task_metrics': metrics}, ensure_ascii=False), flush=True)


def benchmark(args):
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    source = args.source.resolve() if args.source else directory / f'input-{args.rows}.xlsx'
    measurement, task_metrics = Measurement(), []
    report = {
        'status': 'running', 'directory': str(directory), 'source': str(source),
        'input_rows': args.rows, 'expected_accepted_rows': args.rows - args.rows // 50003,
        'expected_rejected_rows': args.rows // 50003,
        'machine': {'os': platform.platform(), 'python': sys.version, 'machine': platform.machine(),
                    'cpu_logical': psutil.cpu_count(),
                    'memory_total_gib': round(psutil.virtual_memory().total / 1024**3, 2)},
        'host': 'source Python TaskManager, Project, and QueryService',
        'worker': str(args.bundle.resolve() / 'TraceAtlasWorker.exe') if args.bundle else 'source Python worker',
        'worker_environment': 'sanitized PATH; Python environment variables removed' if args.bundle else 'inherited',
        'stages': measurement.stages, 'tasks': task_metrics,
        'limitations': [
            'Developer machine, not clean Windows or a 16GiB machine acceptance claim',
            'No packaged GUI is launched; host scheduler and queries run in the source environment',
            'Synthetic manual-mapping XLSX fixture only; no claim about all bank formats',
            'Host RSS sampled at 25ms; worker tree RSS uses TaskManager sampling and can miss brief peaks',
            'Worker RSS and Windows job committed-memory budget measure different quantities',
            'No overall response-time acceptance threshold has been approved',
        ],
    }
    project = manager = None
    try:
        if args.source:
            if not source.is_file():
                raise FileNotFoundError(source)
        else:
            with measurement.stage('generate_xlsx'):
                generate_xlsx(source, args.rows)
        with measurement.stage('create_project'):
            project = Project.create(directory / '调度验证项目 01', f'{args.rows:,} 行真实后台任务验证')
        with worker_environment(args.bundle):
            manager = TaskManager(project)

            def job(label, feature, parameters, inputs=None):
                return run_task(manager, measurement, task_metrics, label, feature,
                                parameters, inputs or [], args.timeout)

            job('import', 'xlsx.import', {
                'path': str(source), 'sheet': '流水', 'header_row': 1,
                'mapping': {'owner_account': 1, 'amount': 2, 'occurred_at': 3,
                            'counterparty_account': 4, 'transaction_id': 5, 'currency': 6},
                'platform': 'bank', 'account_namespace': 'account', 'allow_partial': True,
            })
            accepted, rejected = args.rows - args.rows // 50003, args.rows // 50003
            datasets = project.objects('dataset')
            assert len(datasets) == 1
            dataset = datasets[0]
            assert dataset['data']['row_count'] == accepted
            assert dataset['data']['rejected_count'] == rejected
            assert dataset['data']['input_count'] == args.rows
            report.update(accepted_rows=accepted, rejected_rows=rejected)
            job('account_count', 'validation.account_count', {}, [dataset['id']])
            results = project.objects('result')
            assert len(results) == 1
            result = results[0]
            assert result['data']['metrics']['contributing_count'] == accepted
            expected = expected_account_counts(args.rows)
            assert result['data']['row_count'] == len(expected)
            report['result_rows'] = len(expected)
            query = QueryService(project)
            with measurement.stage('result_pages_verify_all_account_counts'):
                observed = {}
                for offset in range(0, len(expected), 2000):
                    page = query.page(result['id'], sort='owner_account', offset=offset, limit=2000)
                    assert page['total'] == len(expected)
                    observed.update((row['owner_account'], row['record_count']) for row in page['rows'])
                assert observed == expected
            with measurement.stage('dataset_filter_and_paged_sources'):
                page = query.page(result['id'], sort='owner_account', limit=1)
                first = page['rows'][0]
                filtered = query.page(dataset['id'], [
                    {'column': 'owner_account', 'op': 'eq', 'value': first['owner_account']}])
                assert filtered['total'] == first['record_count']
                lineage = []
                for offset in range(0, first['record_count'], 25):
                    sources = query.sources(result['id'], first['result_row_id'], offset=offset, limit=25)
                    assert sources['total'] == first['record_count']
                    lineage.extend(sources['rows'])
                assert len({row['record_id'] for row in lineage}) == first['record_count']
                location = lineage[0]
                preview = query.source_preview(location['source_id'], location['source_sheet'], location['source_row'])
                original = next(row for row in preview['rows'] if row['row_number'] == location['source_row'])
                assert original['column_1'] == first['owner_account']
                assert all(row['dataset_version'] == dataset['id'] for row in lineage)
            job('index_rebuild', 'index.rebuild', {}, [dataset['id']])
            with measurement.stage('index_search'):
                found = query.search_index(first['owner_account'], limit=25)
                assert not found['stale'] and found['total'] > 0
                assert all(row['dataset_version'] == dataset['id'] for row in found['rows'])
            destination = directory / 'records.csv'
            job('export_all_records_csv', 'result.export', {
                'object_id': dataset['id'], 'format': 'csv', 'scope': 'all',
                'destination': str(destination), 'sort': 'source_row',
            }, [dataset['id']])
            with measurement.stage('verify_complete_csv_and_metadata'):
                count, previous = 0, 1
                with destination.open(encoding='utf-8-sig', newline='') as stream:
                    reader = csv.DictReader(stream)
                    for row in reader:
                        count += 1
                        source_row = int(row['source_row'])
                        assert source_row > previous
                        previous = source_row
                        if count == 1:
                            assert row['owner_account'] == '00001'
                            assert row['amount'] == '1.123456789012'
                            assert row['transaction_id'] == '6222020000000000001'
                assert count == accepted
                metadata = json.loads(Path(str(destination) + '.metadata.json').read_text(encoding='utf-8'))
                assert metadata['row_count'] == accepted and metadata['scope'] == 'all'
                assert metadata['dataset_versions'] == [dataset['id']]
                report['exported_rows'] = count
            project_path = project.path
            manager.shutdown()
            manager = None
            project.close()
            project = None
            with measurement.stage('reopen_verify'), Project.open(project_path) as reopened:
                reopened_query = QueryService(reopened)
                assert len(reopened.objects('dataset')) == 1
                assert reopened_query.page(result['id'], limit=1)['total'] == len(expected)
                assert reopened_query.page(dataset['id'], limit=1)['total'] == accepted
                assert not reopened_query.search_index(first['owner_account'], limit=1)['stale']
                assert len(reopened.list_tasks()) == 4
                assert all(task['state'] == 'succeeded' for task in reopened.list_tasks())
                assert all(obj.get('state') in (None, 'available', 'valid', 'ready', 'committed')
                           for obj in reopened.objects())
                # Completed attempt directories may remain for explicit cleanup;
                # their informational recovery notice is not data corruption.
                report['reopen_recovery_notices'] = reopened.recovery_report
        report['status'] = 'passed'
        return report
    except BaseException as exc:
        report.update(status='failed', error={'type': type(exc).__name__, 'message': str(exc)})
        raise
    finally:
        try:
            if manager:
                manager.shutdown(cancel=True)
        finally:
            if project:
                project.close()
            report['host_peak_sampled_rss_mib'] = max(
                (stage['peak_rss_mib'] for stage in measurement.stages), default=0)
            report['worker_tree_peak_sampled_rss_mib'] = max(
                (task['worker_tree_peak_rss_mib'] for task in task_metrics), default=0)
            report['disk_total_bytes_including_staging_and_exports'] = sum(
                path.stat().st_size for path in directory.rglob('*') if path.is_file())
            (directory / 'metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'status': report['status'], 'metrics': str(directory / 'metrics.json')},
                             ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=1000000)
    parser.add_argument('--source', type=Path, help='Existing benchmark_data synthetic XLSX with matching --rows')
    parser.add_argument('--bundle', type=Path, help='Completed bundle directory containing TraceAtlasWorker.exe')
    parser.add_argument('--directory', type=Path, default=Path('artifacts') / (
        'scheduler-benchmark-' + time.strftime('%Y%m%d-%H%M%S')))
    parser.add_argument('--timeout', type=float, default=3600, help='Maximum seconds per task (default: 3600)')
    args = parser.parse_args()
    if args.rows < 1 or args.timeout <= 0:
        parser.error('--rows and --timeout must be positive')
    if args.bundle and os.name != 'nt':
        parser.error('--bundle is supported only on Windows')
    benchmark(args)


if __name__ == '__main__':
    main()
