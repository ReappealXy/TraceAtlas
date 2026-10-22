"""Exercise the shipped worker in an environment without Python on PATH."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import openpyxl

from traceatlas.project import Project
from traceatlas.query import QueryService
from traceatlas.tasks import TaskManager


def wait(manager, tid):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        task = manager.project.get_task(tid)
        if task['state'] not in {'queued', 'running', 'committing', 'cancel_requested'}:
            if task['state'] != 'succeeded':
                raise RuntimeError(json.dumps(task, ensure_ascii=False))
            return task
        time.sleep(0.05)
    raise TimeoutError(tid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, default=Path('dist/TraceAtlas'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    destination = args.output or Path('artifacts') / ('bundle-check-' + time.strftime('%Y%m%d-%H%M%S'))
    destination.mkdir(parents=True, exist_ok=False)
    destination = destination.resolve()
    source = destination / '验证流水.xlsx'
    book = openpyxl.Workbook()
    book.active.title = '流水'
    book.active.append(['本方账户', '金额', '时间', '对方账户'])
    for value in ('10.123456789012', '20.01', '30.00'):
        book.active.append(['00123', value, '2026-10-22', '6222000012345678901'])
    book.save(source)
    project = Project.create(destination / '验证项目', '审计项目 01')
    clean_path = os.pathsep.join([str(Path(os.environ['SystemRoot']) / 'System32'), os.environ['SystemRoot']])
    with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable',
            str((args.bundle / 'TraceAtlas.exe').resolve())), patch.dict(os.environ, {'PATH': clean_path}):
        manager = TaskManager(project)
        try:
            start = time.perf_counter()
            wait(manager, manager.submit('xlsx.import', {'path': str(source), 'sheet': '流水',
                 'header_row': 1, 'mapping': {'owner_account': 1, 'amount': 2, 'occurred_at': 3,
                                             'counterparty_account': 4},
                 'platform': 'bank', 'account_namespace': 'bank-account'}))
            dataset = project.objects('dataset')[0]
            wait(manager, manager.submit('validation.account_count', {}, [dataset['id']]))
            result = project.objects('result')[0]
            page = QueryService(project).page(result['id'])
            assert page['rows'][0]['owner_account'] == '00123'
            assert page['rows'][0]['record_count'] == 3
            assert QueryService(project).sources(result['id'], page['rows'][0]['result_row_id'])['total'] == 3
            wait(manager, manager.submit('index.rebuild', {}, [dataset['id']]))
            matches = QueryService(project).search_index('00123')
            assert len([row for row in matches['rows'] if row['value'] == '00123']) == 3
            exported = destination / '验证导出.csv'
            wait(manager, manager.submit('result.export', {'object_id': result['id'], 'scope': 'all',
                 'format': 'csv', 'destination': str(exported), 'overwrite': False}))
            with exported.open(encoding='utf-8-sig', newline='') as stream:
                assert len(list(csv.reader(stream))) == 2
            project.save_session({'selected_object_id': result['id']})
            report = {'passed': True, 'scope': 'frozen worker, production scheduler and storage, clean child PATH',
                      'duration_seconds': time.perf_counter() - start,
                      'bundle': str(args.bundle.resolve()), 'project': str(project.path),
                      'tasks': manager.list_tasks(), 'dataset_id': dataset['id'], 'result_id': result['id']}
        finally:
            manager.shutdown(cancel=True)
            project.close()
    with Project.open(destination / '验证项目') as reopened:
        assert QueryService(reopened).page(result['id'])['rows'][0]['record_count'] == 3
    (destination / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'report': str(destination / 'report.json'),
                      'project': report['project']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
