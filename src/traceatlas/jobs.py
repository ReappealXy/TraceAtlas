"""Versioned, real background jobs. Every product is written only inside staging."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pyarrow as pa
import pyarrow.parquet as pq

from .importer import TEXT_FIELDS, DataError, check_cancelled, import_rows, normalize_row
from .query import TIME_COMPARISON, build_query, query_connection, register_object, table_columns

ADAPTER_VERSION = 'xlsx.manual/1.0.0'
ALGORITHM_VERSION = 'account-count/1.0.0'
CHUNK_SIZE = 4096


def new_id():
    return uuid4().hex


def _now():
    return datetime.now(UTC).isoformat()


def transaction_schema():
    fields = [('record_id', pa.string()), ('source_id', pa.string()), ('source_sheet', pa.string()),
              ('source_row', pa.int64()), ('platform', pa.string()), ('account_namespace', pa.string())]
    fields.extend((name, pa.string()) for name in TEXT_FIELDS)
    fields.extend([('amount', pa.decimal128(38, 12)), ('amount_raw', pa.string()),
                   ('currency', pa.string()), ('balance', pa.decimal128(38, 12)),
                   ('balance_raw', pa.string()), ('occurred_at', pa.string()),
                   ('time_precision', pa.string()), ('timezone', pa.string()),
                   ('direction', pa.string()), ('direction_raw', pa.string()),
                   ('quality_flags', pa.string()), ('raw_cells', pa.string())])
    return pa.schema(fields)


def file_entry(staging, relative, destination, cancelled=None):
    path = staging / relative
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancelled(cancelled)
            digest.update(chunk)
    return {'source': relative, 'destination': destination, 'sha256': digest.hexdigest(),
            'size': path.stat().st_size}


def _copy_input(source, destination, cancelled):
    digest = hashlib.sha256()
    with Path(source).open('rb') as reader, destination.open('xb') as writer:
        while chunk := reader.read(1024 * 1024):
            check_cancelled(cancelled)
            writer.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _input_objects(spec, kind=None):
    result = spec.get('inputs', [])
    if isinstance(result, dict):
        result = list(result.values())
    if kind:
        result = [obj for obj in result if obj['kind'] == kind]
    return result


def _manifest(files=None, objects=None, summary=None):
    return {'files': files or [], 'objects': objects or [], 'summary': summary or {}}


def _import(spec, staging, emit, cancelled):
    parameters = dict(spec['parameters'])
    source_id, dataset_id, version_id, batch_id = (new_id() for _ in range(4))
    name = Path(parameters['path']).name
    emit('copying_source', 0, None, '保存原始工作簿副本')
    content_hash = _copy_input(parameters['path'], staging / 'source.xlsx', cancelled)
    if parameters.get('expected_source_hash') and parameters['expected_source_hash'] != content_hash:
        raise DataError('文件在检查后发生变化，请重新检查并确认导入。', code='SOURCE_CHANGED', stage='copying_source')
    # Always parse the stored snapshot, so an external edit cannot change mid-job data.
    parameters['path'] = str(staging / 'source.xlsx')
    parameters['_temporary_directory'] = str(staging)
    schema = transaction_schema()
    input_count = valid_count = rejected_count = blank_count = warning_count = 0
    errors_sample, warnings_sample, buffer = [], [], []
    errors_file = staging / 'errors.jsonl'
    writer = pq.ParquetWriter(staging / 'data.parquet', schema, compression='zstd')
    try:
        with (errors_file.open('w', encoding='utf-8') as errors_stream,
              import_rows(parameters, cancelled) as (book, sheet, rows, mapping)):
            estimate = book.estimated_rows(sheet)
            emit('validating', 0, estimate or None, '检查字段、金额和来源位置')
            for source_row, cells in rows:
                input_count += 1
                if not any(cell['value'] is not None or cell.get('formula') for cell in cells.values()):
                    blank_count += 1
                    errors_stream.write(json.dumps({'row': source_row, 'sheet': sheet,
                                                    'disposition': 'blank', 'errors': []}, ensure_ascii=False) + '\n')
                    continue
                record, errors, warnings = normalize_row(cells, mapping, parameters, book.date1904)
                warning_count += len(warnings)
                if len(warnings_sample) < 200:
                    warnings_sample.extend({'row': source_row, **issue} for issue in warnings[:200-len(warnings_sample)])
                if errors:
                    rejected_count += 1
                    item = {'row': source_row, 'sheet': sheet, 'errors': errors,
                            'disposition': 'rejected', 'raw_cells': cells}
                    errors_stream.write(json.dumps(item, ensure_ascii=False) + '\n')
                    if len(errors_sample) < 200:
                        errors_sample.extend({'row': source_row, **issue} for issue in errors[:200-len(errors_sample)])
                else:
                    valid_count += 1
                    record.update(record_id=new_id(), source_id=source_id,
                                  source_sheet=sheet, source_row=source_row)
                    buffer.append(record)
                    if len(buffer) >= CHUNK_SIZE:
                        check_cancelled(cancelled)
                        writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
                        buffer.clear()
                if input_count % CHUNK_SIZE == 0:
                    emit('importing', input_count, None, f'已检查 {input_count:,} 行；合格 {valid_count:,}，错误 {rejected_count:,}')
            if buffer:
                writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
            if valid_count == 0:
                # Parquet remains a real, readable zero-row table.
                writer.write_table(pa.Table.from_pylist([], schema=schema))
    finally:
        writer.close()
    check_cancelled(cancelled)
    report = {'input_count': input_count, 'row_count': valid_count, 'valid_count': valid_count,
              'rejected_count': rejected_count, 'blank_count': blank_count, 'warning_count': warning_count,
              'errors': errors_sample, 'warnings': warnings_sample,
              'completion': 'partial' if rejected_count else 'complete'}
    if rejected_count and not parameters.get('allow_partial', False):
        raise DataError(f'发现 {rejected_count:,} 行错误，本次未提交。请查看质量报告，修正后重试，或明确允许只导入合格行。',
                        code='IMPORT_VALIDATION', stage='validation', details=report)
    data_path = f'datasets/{dataset_id}/{version_id}/data.parquet'
    source_path = f'sources/{source_id}/source.xlsx'
    errors_path = f'datasets/{dataset_id}/{version_id}/errors.jsonl'
    report_path = f'datasets/{dataset_id}/{version_id}/quality.json'
    (staging / 'quality.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    emit('verifying', valid_count, valid_count, '校验数据与原文件摘要')
    files = [file_entry(staging, 'source.xlsx', source_path, cancelled),
             file_entry(staging, 'data.parquet', data_path, cancelled),
             file_entry(staging, 'errors.jsonl', errors_path, cancelled),
             file_entry(staging, 'quality.json', report_path, cancelled)]
    snapshot = dict(spec['parameters'])
    # Paths are deliberately kept in source metadata only as a display origin, not a dependency.
    objects = [
        {'id': source_id, 'kind': 'source', 'name': name,
         'data': {'stored_path': source_path, 'content_hash': content_hash,
                  'media_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                  'display_name': name}},
        {'id': batch_id, 'kind': 'batch', 'name': f'导入 {name}',
         'data': {'batch_id': batch_id, 'source_ids': [source_id], 'adapter_version': ADAPTER_VERSION,
                  'mapping_snapshot': snapshot, 'dataset_versions': [version_id], **report}},
        {'id': version_id, 'kind': 'dataset', 'name': f'{name} · {sheet}',
         'data': {'dataset_id': dataset_id, 'version_id': version_id, 'schema_id': 'transactions',
                  'schema_version': 1, 'parents': [], 'source_id': source_id, 'batch_id': batch_id,
                  'row_count': valid_count, 'input_count': input_count, 'rejected_count': rejected_count,
                  'blank_count': blank_count, 'sheet': sheet, 'mapping': snapshot,
                  'data_path': data_path, 'errors_path': errors_path, 'quality_path': report_path,
                  'adapter_version': ADAPTER_VERSION, 'quality_report': report}},
    ]
    return _manifest(files, objects, report)


@contextmanager
def job_connection(staging, cancelled):
    """Interrupt DuckDB promptly as well as at Python chunk boundaries."""
    done = threading.Event()
    spill = staging / '.spill'
    with query_connection(spill) as connection:
        def watch():
            while not done.wait(0.1):
                if cancelled():
                    connection.interrupt()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            yield connection
        except Exception:
            check_cancelled(cancelled)
            raise
        finally:
            done.set()
            watcher.join(timeout=1)
    if spill.exists():
        shutil.rmtree(spill)


def _register_datasets(connection, spec):
    inputs = _input_objects(spec, 'dataset')
    if not inputs:
        raise DataError('请先选择至少一个标准数据集。', code='INPUT_REQUIRED')
    unions = []
    for index, obj in enumerate(inputs):
        view = f'input_{index}'
        register_object(connection, spec['project_path'], obj, view)
        # IDs originate from registered project metadata, still escaped as literals.
        version = obj['id'].replace("'", "''")
        unions.append(f"SELECT *, '{version}' AS dataset_version FROM {view}")
    connection.execute('CREATE VIEW input_records AS ' + ' UNION ALL '.join(unions))
    return inputs


def _copy_query(connection, sql, path):
    escaped = str(path).replace("'", "''")
    connection.execute(f"COPY ({sql}) TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)")


def _count(spec, staging, emit, cancelled):
    result_id = new_id()
    with job_connection(staging, cancelled) as connection:
        inputs = _register_datasets(connection, spec)
        emit('counting', 0, None, '按平台、账户类别和本方账户统计输入记录数')
        connection.execute('''CREATE TABLE counted AS
            SELECT CAST(uuid() AS VARCHAR) AS result_row_id, platform, account_namespace,
                   owner_account, count(*)::BIGINT AS record_count
            FROM input_records GROUP BY platform, account_namespace, owner_account''')
        check_cancelled(cancelled)
        row_count, contributing = connection.execute('SELECT count(*), coalesce(sum(record_count),0) FROM counted').fetchone()
        _copy_query(connection, 'SELECT * FROM counted ORDER BY platform, account_namespace, owner_account', staging / 'result.parquet')
        emit('lineage', 0, int(contributing), '保存每条结果与全部贡献记录的关系')
        _copy_query(connection, '''SELECT c.result_row_id, r.record_id, r.dataset_version,
                          r.source_id, r.source_sheet, r.source_row
                   FROM input_records r JOIN counted c
                   ON r.platform=c.platform AND r.account_namespace=c.account_namespace
                   AND r.owner_account=c.owner_account''', staging / 'lineage.parquet')
        lineage_count = pq.ParquetFile(staging / 'lineage.parquet').metadata.num_rows
        if lineage_count != int(contributing):
            raise DataError('来源关系数量与贡献记录数不一致，结果未提交。', code='LINEAGE_INVALID')
        columns = table_columns(connection.execute('DESCRIBE counted').fetchall())
    check_cancelled(cancelled)
    data_path, lineage_path = f'results/{result_id}/data.parquet', f'results/{result_id}/lineage.parquet'
    versions = [obj['id'] for obj in inputs]
    result = {'id': result_id, 'kind': 'result', 'name': '按账户计数', 'data': {
        'result_id': result_id, 'task_id': spec['task_id'], 'attempt_id': spec['attempt_id'],
        'feature_id': spec['feature_id'], 'created_at': _now(), 'data_path': data_path,
        'lineage_path': lineage_path, 'row_count': row_count, 'dataset_versions': versions,
        'parameter_snapshot': spec['parameters'], 'algorithm_version': ALGORITHM_VERSION,
        'module_version': spec.get('module_version', '1.0.0'), 'completion': 'complete',
        'columns': columns, 'tables': [{'data_path': data_path, 'columns': columns}], 'graphs': [],
        'metrics': {'input_count': int(contributing), 'contributing_count': int(contributing),
                    'excluded_count': 0, 'group_count': row_count},
        'warnings': ['这是输入记录数，不是去重后的经济交易数；重复导入会分别计数。'],
        'provenance_ref': lineage_path,
    }}
    return _manifest([file_entry(staging, 'result.parquet', data_path, cancelled),
                      file_entry(staging, 'lineage.parquet', lineage_path, cancelled)], [result],
                     {'row_count': row_count, 'contributing_count': int(contributing)})


def _index(spec, staging, emit, cancelled):
    index_id = new_id()
    with job_connection(staging, cancelled) as connection:
        inputs = _register_datasets(connection, spec)
        emit('indexing', 0, None, '建立项目内账户与对方账户字段索引')
        sql = '''SELECT CAST(json_array(platform,account_namespace) AS VARCHAR) AS namespace,
                    owner_account AS value, 'owner_account' AS field, dataset_version, record_id,
                    source_id, source_sheet, source_row FROM input_records
                 WHERE owner_account IS NOT NULL AND owner_account <> ''
                 UNION ALL
                 SELECT CAST(json_array(platform,account_namespace) AS VARCHAR) AS namespace,
                    counterparty_account AS value, 'counterparty_account' AS field, dataset_version, record_id,
                    source_id, source_sheet, source_row FROM input_records
                 WHERE counterparty_account IS NOT NULL AND counterparty_account <> '' '''
        _copy_query(connection, sql, staging / 'index.parquet')
        count = pq.ParquetFile(staging / 'index.parquet').metadata.num_rows
    path = f'indexes/{index_id}/index.parquet'
    return _manifest([file_entry(staging, 'index.parquet', path, cancelled)], [
        {'id': index_id, 'kind': 'index', 'name': '项目账户索引', 'data': {
            'data_path': path, 'dataset_versions': [obj['id'] for obj in inputs],
            'row_count': count, 'algorithm_version': 'account-index/1.0.0',
            'created_at': _now(), 'normalization': 'identity; namespace=[platform,account_namespace]',
        }}], {'row_count': count})


def _export_text(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return format(value, 'f')
    return str(value)


def _write_xlsx(target, columns, total, cursor, emit, cancelled):
    """Stream standards-compliant inline text cells directly to the staged ZIP.

    No spreadsheet-wide shared-string dictionary or system-temp worksheet copy is
    needed. Excel text limits are checked before writing instead of silently truncating.
    """
    sheet_limit = 1048575
    sheet_count = max(1, (total + sheet_limit - 1) // sheet_limit)
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    package_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    content_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    prefixes = []
    for number in range(1, len(columns) + 1):
        prefix = ''
        while number:
            number, remainder = divmod(number - 1, 26)
            prefix = chr(65 + remainder) + prefix
        prefixes.append(prefix)

    def xml_row(row_number, values):
        cells = []
        for index, value in enumerate(values):
            text = _export_text(value)
            if len(text) > 32767:
                raise DataError(f'导出第 {row_number} 行的 {columns[index]} 超过 Excel 单元格 32767 字符限制；'
                                '本次未发布，请改用 CSV，避免截断原文。', code='EXPORT_CELL_LIMIT')
            if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', text):
                raise DataError('单元格包含 Excel 无法保存的控制字符；请改用 CSV 保留原文。', code='EXPORT_CELL_CHAR')
            cells.append(f'<c r="{prefixes[index]}{row_number}" t="inlineStr"><is>'
                         f'<t xml:space="preserve">{escape(text)}</t></is></c>')
        return (f'<row r="{row_number}">' + ''.join(cells) + '</row>').encode('utf-8')

    count = 0
    with ZipFile(target, 'x', ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                            for i in range(1, sheet_count + 1))
        archive.writestr('[Content_Types].xml', f'<Types xmlns="{content_ns}">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/xl/workbook.xml" '
                         'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                         f'{overrides}</Types>')
        archive.writestr('_rels/.rels', f'<Relationships xmlns="{package_ns}">'
                         f'<Relationship Id="rId1" Type="{rel_ns}/officeDocument" Target="xl/workbook.xml"/>'
                         '</Relationships>')
        sheets = ''.join(f'<sheet name="数据 {i}" sheetId="{i}" r:id="rId{i}"/>'
                         for i in range(1, sheet_count + 1))
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="{rel_ns}"><sheets>{sheets}</sheets></workbook>')
        relationships = ''.join(f'<Relationship Id="rId{i}" Type="{rel_ns}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                                for i in range(1, sheet_count + 1))
        archive.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="{package_ns}">{relationships}</Relationships>')
        pending = iter(())
        for sheet_index in range(1, sheet_count + 1):
            with archive.open(f'xl/worksheets/sheet{sheet_index}.xml', 'w', force_zip64=True) as stream:
                stream.write(f'<worksheet xmlns="{ns}"><sheetData>'.encode())
                stream.write(xml_row(1, columns))
                target_rows = min(sheet_limit, total - count)
                for row_number in range(2, target_rows + 2):
                    values = next(pending, None)
                    if values is None:
                        check_cancelled(cancelled)
                        batch = cursor.fetchmany(CHUNK_SIZE)
                        if not batch:
                            raise DataError('导出条数与快照不符。', code='EXPORT_COUNT_MISMATCH')
                        pending = iter(batch)
                        values = next(pending)
                    stream.write(xml_row(row_number, values))
                    count += 1
                    if count % CHUNK_SIZE == 0:
                        emit('exporting', count, total, f'已导出 {count:,} 条')
                stream.write(b'</sheetData></worksheet>')
    return count, sheet_count


def _export(spec, staging, emit, cancelled):
    params = spec['parameters']
    object_id = params.get('object_id')
    obj = next((obj for obj in _input_objects(spec) if obj['id'] == object_id), None)
    if obj is None:
        raise DataError('导出对象未登记在任务输入快照中。', code='INPUT_REQUIRED')
    format_name = params.get('format', 'xlsx')
    if format_name not in ('xlsx', 'csv'):
        raise DataError('导出格式必须是 xlsx 或 csv。', code='FORMAT_INVALID')
    scope = params.get('scope', 'filtered')
    if scope not in ('filtered', 'all'):
        raise DataError('导出范围必须是当前筛选或全部。', code='SCOPE_INVALID')
    destination = Path(params['destination']).expanduser().resolve()
    if destination.suffix.lower() != '.' + format_name:
        raise DataError('导出路径的扩展名与所选格式不一致。', code='FORMAT_INVALID')
    metadata_destination = Path(str(destination) + '.metadata.json')
    if not params.get('overwrite', False) and (destination.exists() or metadata_destination.exists()):
        raise DataError('目标文件或元数据清单已存在；请选择其他路径或明确允许覆盖。', code='EXPORT_EXISTS')
    relative = 'export.' + format_name
    target = staging / relative
    with job_connection(staging, cancelled) as connection:
        description = register_object(connection, spec['project_path'], obj)
        filters = params.get('filters') if scope == 'filtered' else []
        sql, values, order = build_query(description, filters, params.get('sort'), params.get('descending', False))
        total = connection.execute(f'SELECT count(*) FROM ({sql}) counted', values).fetchone()[0]
        cursor = connection.execute(f'{sql} ORDER BY {order}', values)
        columns = [col[0] for col in description]
        count, sheets = 0, 1
        emit('exporting', 0, total, f'导出 {total:,} 条记录')
        if format_name == 'csv':
            with target.open('x', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream, delimiter=',', lineterminator='\r\n')
                writer.writerow(columns)
                while rows := cursor.fetchmany(CHUNK_SIZE):
                    check_cancelled(cancelled)
                    writer.writerows([_export_text(value) for value in row] for row in rows)
                    count += len(rows)
                    emit('exporting', count, total, f'已导出 {count:,} 条')
        else:
            count, sheets = _write_xlsx(target, columns, total, cursor, emit, cancelled)
        if count != total:
            raise DataError('导出条数与查询快照不一致，未发布文件。', code='EXPORT_COUNT_MISMATCH')
    check_cancelled(cancelled)
    export_id = new_id()
    data_entry = file_entry(staging, relative, str(destination), cancelled)
    data_entry['overwrite'] = bool(params.get('overwrite', False))
    metadata = {
        'export_id': export_id, 'task_id': spec['task_id'], 'attempt_id': spec['attempt_id'],
        'object_id': object_id, 'created_at': _now(), 'row_count': count, 'sheet_count': sheets,
        'scope': scope, 'filters': filters or [], 'sort': params.get('sort'),
        'descending': params.get('descending', False), 'format': format_name,
        'encoding': 'UTF-8 BOM' if format_name == 'csv' else 'OOXML',
        'delimiter': ',' if format_name == 'csv' else None, 'decimal_representation': 'exact text, 12 decimal places',
        'dataset_versions': obj['data'].get('dataset_versions', [object_id] if obj['kind'] == 'dataset' else []),
        'algorithm_version': obj['data'].get('algorithm_version', obj['data'].get('adapter_version')),
        'parameter_snapshot': obj['data'].get('parameter_snapshot', obj['data'].get('mapping')),
        'output_path': str(destination), 'sha256': data_entry['sha256'],
        'columns': table_columns(description),
    }
    if params.get('sort') == 'occurred_at' or any(item.get('column') == 'occurred_at' for item in filters or []):
        metadata['time_comparison'] = dict(TIME_COMPARISON)
    (staging / 'export.metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    meta_entry = file_entry(staging, 'export.metadata.json', str(metadata_destination), cancelled)
    meta_entry['overwrite'] = data_entry['overwrite']
    return {'files': [], 'exports': [data_entry, meta_entry],
            'objects': [{'id': export_id, 'kind': 'export', 'name': destination.name, 'data': metadata}],
            'summary': {'row_count': count, 'destination': str(destination), 'metadata_path': str(metadata_destination),
                        'sha256': data_entry['sha256'], 'sheet_count': sheets}}


JOBS = {'xlsx.import': _import, 'validation.account_count': _count,
        'result.export': _export, 'index.rebuild': _index}


def execute_job(spec: dict, staging: Path, emit, cancelled):
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    check_cancelled(cancelled)
    feature = spec.get('feature_id')
    if feature not in JOBS:
        raise DataError(f'当前版本无法执行此功能：{feature}', code='FEATURE_UNSUPPORTED')
    return JOBS[feature](spec, staging, emit, cancelled)
