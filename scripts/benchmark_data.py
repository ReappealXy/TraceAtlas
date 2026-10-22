"""Reproducible A data-workflow benchmark (real XLSX and real project commits).

Run: uv run python scripts/benchmark_data.py
Outputs are local artifacts; this intentionally does not claim clean-Windows or
TaskManager resource-isolation validation. Run each size in a fresh process.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import psutil

from traceatlas.jobs import execute_job
from traceatlas.project import Project
from traceatlas.query import QueryService


class Measurement:
    def __init__(self):
        self.stages = []

    @contextmanager
    def stage(self, name):
        stop = threading.Event()
        process = psutil.Process()
        baseline = process.memory_info().rss
        peak = [baseline]
        def monitor():
            while not stop.wait(.025):
                peak[0] = max(peak[0], process.memory_info().rss)
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        start = time.perf_counter()
        status = 'passed'
        try:
            yield
        except BaseException:
            status = 'failed'
            raise
        finally:
            stop.set()
            thread.join()
            item = {'stage': name, 'elapsed_seconds': round(time.perf_counter() - start, 4),
                    'baseline_rss_mib': round(baseline / 1024**2, 2),
                    'peak_rss_mib': round(max(peak[0], process.memory_info().rss) / 1024**2, 2),
                    'status': status}
            self.stages.append(item)
            print(json.dumps(item), flush=True)


def generate_xlsx(path, rows):
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with ZipFile(path, 'x', ZIP_DEFLATED, compresslevel=3) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                         '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'
                         '<sheet name="流水" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        with archive.open('xl/worksheets/sheet1.xml', 'w', force_zip64=True) as stream:
            stream.write(f'<worksheet xmlns="{ns}"><dimension ref="A1:G{rows+1}"/><sheetData>'.encode())
            header = ['本方账户', '金额', '交易时间', '对方账户', '流水号', '币种', '备注']
            cells = ''.join(f'<c r="{chr(65+j)}1" t="inlineStr"><is><t>{value}</t></is></c>' for j, value in enumerate(header))
            stream.write(f'<row r="1">{cells}</row>'.encode())
            block = []
            for index in range(1, rows + 1):
                # A few exact duplicated business rows remain independent input records.
                value_index = index - 1 if index % 200007 == 0 else index
                amount = 'bad' if index % 50003 == 0 else f'{value_index % 10000}.123456789012'
                values = [f'{value_index % 10000:05d}', amount, '2026-10-22 12:34:56.123',
                          f'{(value_index+1) % 10000:05d}', f'622202{value_index:013d}',
                          'CNY', 'synthetic-benchmark']
                row = index + 1
                cells = ''.join(f'<c r="{chr(65+j)}{row}" t="inlineStr"><is><t>{value}</t></is></c>' for j, value in enumerate(values))
                block.append(f'<row r="{row}">{cells}</row>')
                if len(block) == 4096:
                    stream.write(''.join(block).encode())
                    block.clear()
            if block:
                stream.write(''.join(block).encode())
            stream.write(b'</sheetData></worksheet>')


def benchmark(size, directory):
    directory.mkdir(parents=True, exist_ok=False)
    measurement = Measurement()
    source = directory / f'input-{size}.xlsx'
    with measurement.stage('generate_xlsx'):
        generate_xlsx(source, size)
    with measurement.stage('create_project'):
        project = Project.create(directory / '审计项目 01', f'{size:,} 行验证')

    def job(label, feature, parameters, inputs):
        spec = {'task_id': uuid4().hex, 'attempt_id': uuid4().hex, 'project_id': project.id,
                'project_path': str(project.path), 'feature_id': feature, 'module_version': '1.0.0',
                'parameters': parameters, 'inputs': inputs, 'input_dataset_versions': [obj['id'] for obj in inputs]}
        project.create_task(spec)
        project.update_task(spec['task_id'], 'running')
        staging = project.path / 'staging' / spec['attempt_id']
        with measurement.stage(label + '_worker'):
            manifest = execute_job(spec, staging, lambda *args: None, lambda: False)
        with measurement.stage(label + '_commit'):
            project.commit(manifest, spec['task_id'], spec['attempt_id'])
        return manifest

    manifest = job('import', 'xlsx.import', {
        'path': str(source), 'sheet': '流水', 'header_row': 1,
        'mapping': {'owner_account': 1, 'amount': 2, 'occurred_at': 3,
                    'counterparty_account': 4, 'transaction_id': 5, 'currency': 6},
        'platform': 'bank', 'account_namespace': 'account', 'allow_partial': True,
    }, [])
    accepted = size - size // 50003
    assert manifest['summary']['row_count'] == accepted
    assert manifest['summary']['rejected_count'] == size // 50003
    dataset = project.objects('dataset')[0]
    manifest = job('account_count', 'validation.account_count', {}, [dataset])
    assert manifest['summary']['contributing_count'] == accepted
    result = project.objects('result')[0]
    query = QueryService(project)
    with measurement.stage('first_result_page'):
        page = query.page(result['id'], sort='owner_account')
        assert page['total'] == 10000
    with measurement.stage('dataset_filter_and_source_page'):
        filtered = query.page(dataset['id'], [{'column': 'owner_account', 'op': 'eq', 'value': '00123'}])
        assert filtered['total'] > 0
        sources = query.sources(result['id'], page['rows'][0]['result_row_id'])
        assert sources['total'] == page['rows'][0]['record_count']
        location = sources['rows'][0]
        preview = query.source_preview(location['source_id'], location['source_sheet'], location['source_row'])
        assert any(row['row_number'] == location['source_row'] for row in preview['rows'])
    job('export_result_xlsx', 'result.export', {
        'object_id': result['id'], 'format': 'xlsx', 'scope': 'all',
        'destination': str(directory / 'result.xlsx'),
    }, [result])
    manifest = job('export_dataset_csv', 'result.export', {
        'object_id': dataset['id'], 'format': 'csv', 'scope': 'all',
        'destination': str(directory / 'records.csv'), 'sort': 'source_row',
    }, [dataset])
    assert manifest['summary']['row_count'] == accepted
    # No embedded newline is used in this generated fixture; metadata and source
    # texts are still checked separately by integration tests.
    with measurement.stage('verify_export_count'), (directory / 'records.csv').open('rb') as stream:
        assert sum(1 for _ in stream) - 1 == accepted
    job('index', 'index.rebuild', {}, [dataset])
    with measurement.stage('index_search'):
        found = query.search_index('00123')
        assert not found['stale'] and found['total'] > 0
    project_path = project.path
    project.close()
    with measurement.stage('reopen_verify'):
        project = Project.open(project_path)
        assert QueryService(project).page(result['id'])['total'] == 10000
        assert len(project.objects('dataset')) == 1
        project.close()
    disk = {str(path.relative_to(directory)): path.stat().st_size
            for path in directory.rglob('*') if path.is_file()}
    result = {'input_rows': size, 'accepted_rows': accepted, 'rejected_rows': size // 50003,
              'result_rows': 10000, 'scope': 'direct execute_job plus real Project transaction; no TaskManager isolation',
              'directory': str(directory), 'stages': measurement.stages,
              'peak_sampled_rss_mib': max(stage['peak_rss_mib'] for stage in measurement.stages),
              'disk_total_bytes_including_staging_and_exports': sum(disk.values()), 'disk_files': disk,
              'status': 'passed'}
    (directory / 'metrics.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', type=int)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--sizes', nargs='+', type=int, default=[100000, 1000000])
    args = parser.parse_args()
    if args.child:
        benchmark(args.child, args.directory)
        return
    base = Path('artifacts') / ('data-benchmark-' + time.strftime('%Y%m%d-%H%M%S'))
    base.mkdir(parents=True)
    results = []
    for size in args.sizes:
        directory = base / str(size)
        subprocess.run([sys.executable, __file__, '--child', str(size), '--directory', str(directory.resolve())], check=True)
        results.append(json.loads((directory / 'metrics.json').read_text(encoding='utf-8')))
    machine = {'os': platform.platform(), 'python': sys.version, 'machine': platform.machine(),
               'cpu_logical': psutil.cpu_count(), 'memory_total_gib': round(psutil.virtual_memory().total / 1024**3, 2)}
    output = {'machine': machine, 'runs': results,
              'limitations': ['Developer machine, not clean Windows acceptance',
                              'Direct jobs share benchmark process; TaskManager memory enforcement measured separately',
                              'Synthetic XLSX fixture, not a claim about all bank formats',
                              'Peak RSS sampled at 25ms; no overall response-time acceptance threshold has been approved']}
    Path('artifacts/data-benchmark.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
