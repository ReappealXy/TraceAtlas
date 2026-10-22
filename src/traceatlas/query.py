"""Read-only paged queries shared by the desktop and export worker."""
from __future__ import annotations

import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import duckdb

from .importer import DataError, WorkbookReader, display_cell

LABELS = {
    'record_id': '记录 ID', 'result_row_id': '结果行 ID', 'dataset_version': '数据版本',
    'source_id': '来源 ID', 'source_sheet': '原工作表', 'source_row': '原始行号',
    'platform': '平台', 'account_namespace': '账户类别', 'owner_account': '本方账户',
    'counterparty_account': '对方账户', 'owner_name': '本方名称', 'counterparty_name': '对方名称',
    'transaction_id': '流水号', 'amount': '金额', 'amount_raw': '原始金额', 'currency': '币种',
    'balance': '余额', 'balance_raw': '原始余额', 'occurred_at': '交易时间',
    'time_precision': '时间精度', 'timezone': '时区', 'direction': '方向', 'direction_raw': '原始方向',
    'status': '交易状态', 'transaction_type': '交易类型', 'channel': '渠道', 'ip': 'IP',
    'device_id': '设备编号', 'location': '地点', 'quality_flags': '质量提示', 'raw_cells': '原单元格',
    'record_count': '输入记录数', 'namespace': '命名空间', 'value': '字段值',
    'field': '字段', 'row_number': '原始行号',
}

# Compare whole seconds and a normalized fractional string separately. DuckDB's
# TIMESTAMPTZ rounds to microseconds, and TIMESTAMP_NS has a limited year range.
# Lexical comparison of fractions after stripping trailing zeros is exact even
# when the source contains more than nine decimal places.
_TIME_PATTERN = (r'^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2})'
                 r'(?:[.,](\d+))?)?(Z|[+-]\d{2}(?::?\d{2})?)?$')
_OFFSET_PATTERN = r'^[+-]([01]\d|2[0-3])([0-5]\d)?$'
TIME_COMPARISON = {
    'version': 'time-query/1.0.0',
    'date': 'original calendar date; no timezone conversion',
    'precise': 'known offsets compare instants; unknown zones compare wall times; categories never mix',
    'minute': 'minute-only values compare at minute start; original precision is retained',
    'date_only': 'excluded from precise comparisons',
    'sort': 'fixed groups: known offset, unknown zone, date only, unsupported, missing; order within group',
    'unsupported': 'unrecognized timezone names or offset forms do not enter precise comparisons',
}


def _time_bound(value):
    """Parse one user-supplied bound without reducing its fractional precision."""
    match = re.fullmatch(_TIME_PATTERN, value) if isinstance(value, str) else None
    if not match:
        raise DataError('请输入年-月-日 时:分[:秒[.小数]]；已知时区请附 +08:00 或 Z。',
                        code='FILTER_TIME_INVALID')
    day, minute, second, fraction, offset = match.groups()
    try:
        local = datetime.fromisoformat(f'{day}T{minute}:{second or "00"}')
        delta = local - datetime(1970, 1, 1)
    except ValueError as exc:
        raise DataError('筛选日期或时间无效。', code='FILTER_TIME_INVALID') from exc
    whole = delta.days * 86400 + delta.seconds
    if offset:
        compact = offset.replace(':', '')
        if compact != 'Z' and not re.fullmatch(_OFFSET_PATTERN, compact):
            raise DataError('筛选时区偏移无效。', code='FILTER_TIME_INVALID')
        if compact != 'Z':
            seconds = int(compact[1:3]) * 3600 + int(compact[3:] or '0') * 60
            whole -= seconds if compact[0] == '+' else -seconds
    return 0 if offset else 1, whole, (fraction or '').rstrip('0')


def _temporal_source(view, types):
    """Vectorized SQL keys, hidden from the displayed and exported columns."""
    prefix = '__traceatlas_time_'
    while any(key.startswith(prefix) for key in types):
        prefix += '_'
    names = {key: quoted(prefix + key) for key in
             ('day', 'minute', 'second', 'fraction', 'offset', 'valid', 'local', 'zone', 'kind', 'whole')}
    original = quoted('occurred_at')
    zone = quoted('timezone') if 'timezone' in types else "'unknown'"
    pattern = "'" + _TIME_PATTERN.replace("'", "''") + "'"
    parsed = [f'regexp_extract({original}, {pattern}, {number}) AS {names[key]}'
              for number, key in enumerate(('day', 'minute', 'second', 'fraction', 'offset'), 1)]
    parsed.append(f'regexp_full_match({original}, {pattern}) AS {names["valid"]}')
    base = f'SELECT *, {", ".join(parsed)} FROM {quoted(view)}'
    local = (f"date_diff('second', TIMESTAMP '1970-01-01 00:00:00', "
             f"TRY_CAST({names['day']} || 'T' || {names['minute']} || ':' || "
             f"coalesce(nullif({names['second']}, ''), '00') AS TIMESTAMP))")
    effective_zone = (
        f"replace(CASE WHEN {names['offset']} <> '' THEN {names['offset']} "
        f"WHEN {zone} IS NULL OR {zone} = '' OR lower({zone}) = 'unknown' THEN '' "
        f"WHEN {zone} = 'Asia/Shanghai' THEN '+0800' "
        f"WHEN {zone} IN ('UTC', 'Z') THEN 'Z' ELSE {zone} END, ':', '')")
    prepared = f'SELECT *, {local} AS {names["local"]}, {effective_zone} AS {names["zone"]} FROM ({base}) time_parts'
    supported = f"({names['zone']} = 'Z' OR regexp_full_match({names['zone']}, '{_OFFSET_PATTERN}'))"
    kind = (f'CASE WHEN {original} IS NULL THEN 4 '
            f"WHEN regexp_full_match({original}, '\\d{{4}}-\\d{{2}}-\\d{{2}}') "
            f'AND TRY_CAST({original} AS DATE) IS NOT NULL THEN 2 '
            f'WHEN NOT coalesce({names["valid"]}, false) OR {names["local"]} IS NULL THEN 3 '
            f"WHEN {names['zone']} = '' THEN 1 WHEN {supported} THEN 0 ELSE 3 END")
    offset = (f"CASE WHEN {names['zone']} IN ('', 'Z') THEN 0 ELSE "
              f"(CASE WHEN left({names['zone']}, 1) = '-' THEN -1 ELSE 1 END) * "
              f"(TRY_CAST(substr({names['zone']}, 2, 2) AS BIGINT) * 3600 + "
              f"coalesce(TRY_CAST(nullif(substr({names['zone']}, 4, 2), '') AS BIGINT), 0) * 60) END")
    source = (f'(SELECT *, {kind} AS {names["kind"]}, {names["local"]} - ({offset}) '
              f'AS {names["whole"]} FROM ({prepared}) time_ready) time_values')
    return source, names


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def project_file(project_path, relative):
    root = Path(project_path).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise DataError('数据引用超出项目目录。', code='INVALID_REFERENCE')
    if not path.is_file():
        raise DataError(f'项目产物缺失：{relative}。请从备份恢复。', code='ARTIFACT_MISSING')
    return path


@contextmanager
def query_connection(temp_path=None):
    temporary = None
    if temp_path is None:
        temporary = tempfile.TemporaryDirectory(prefix='traceatlas-query-')
        temp_path = Path(temporary.name)
    else:
        temp_path = Path(temp_path)
        temp_path.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(':memory:', config={
        'threads': 2, 'memory_limit': '512MB', 'temp_directory': str(temp_path),
        'preserve_insertion_order': False,
    })
    try:
        yield connection
    finally:
        connection.close()
        if temporary:
            temporary.cleanup()


def register_object(connection, project_path, obj, view='records'):
    if obj.get('state') in ('corrupt', 'damaged', 'deleted', 'missing', 'corrupted'):
        raise DataError('所选数据已损坏或删除，不能读取。', code='OBJECT_UNAVAILABLE')
    relative = obj['data'].get('data_path')
    if not relative:
        raise DataError('所选对象没有可查询表格。', code='NO_TABLE')
    path = project_file(project_path, relative)
    connection.read_parquet(str(path)).create_view(view)
    return connection.execute(f'DESCRIBE {quoted(view)}').fetchall()


def build_query(description, filters=None, sort=None, descending=False, view='records'):
    """Whitelist columns/operators; the desktop and export call the exact same builder."""
    types = {row[0]: row[1] for row in description}
    stable = next((key for key in ('result_row_id', 'record_id', 'source_row') if key in types), next(iter(types)))
    sort = sort or stable
    temporal = None
    source = quoted(view)
    if 'occurred_at' in types and (sort == 'occurred_at' or any(
            item.get('column') == 'occurred_at' and item.get('op') in ('eq', 'gte', 'lte', 'gt', 'lt')
            and item.get('value') is not None
            and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(item['value'])) for item in filters or [])):
        source, temporal = _temporal_source(view, types)
    conditions, values = [], []
    for condition in filters or []:
        column, operation = condition.get('column'), condition.get('op')
        if column not in types:
            raise DataError(f'筛选字段不存在：{column}', code='FILTER_INVALID')
        ref, value = quoted(column), condition.get('value')
        if operation == 'is_null':
            conditions.append(f'{ref} IS NULL')
        elif operation == 'contains':
            conditions.append(f'contains(CAST({ref} AS VARCHAR), ?)')
            values.append(str(value))
        elif operation in ('eq', 'gte', 'lte', 'gt', 'lt'):
            operator = {'eq': '=', 'gte': '>=', 'lte': '<=', 'gt': '>', 'lt': '<'}[operation]
            if value is None:
                if operation != 'eq':
                    raise DataError('空值只能使用“为空”筛选。', code='FILTER_INVALID')
                conditions.append(f'{ref} IS NULL')
            elif column == 'occurred_at' and isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise DataError('筛选日期无效。', code='FILTER_INVALID') from exc
                # Calendar filters use the date written in the source, even when
                # its UTC instant belongs to the previous or following day.
                ref = f'substr({ref}, 1, 10)'
                conditions.append(f'{ref} {operator} ?')
                values.append(value)
            elif column == 'occurred_at':
                category, whole, fraction = _time_bound(value)
                seconds_ref, fraction_ref = temporal['whole'], f'rtrim({temporal["fraction"]}, \'0\')'
                if operation == 'eq':
                    comparison = f'{seconds_ref} = ? AND {fraction_ref} = ?'
                    bound_values = [whole, fraction]
                else:
                    seconds_operator = '>' if operation in ('gt', 'gte') else '<'
                    comparison = (f'({seconds_ref} {seconds_operator} ? OR '
                                  f'({seconds_ref} = ? AND {fraction_ref} {operator} ?))')
                    bound_values = [whole, whole, fraction]
                conditions.append(f'({temporal["kind"]} = ? AND {comparison})')
                values.extend([category, *bound_values])
            else:
                conditions.append(f'{ref} {operator} ?')
                values.append(value)
        else:
            raise DataError(f'不支持的筛选方式：{operation}', code='FILTER_INVALID')
    if sort not in types:
        raise DataError(f'排序字段不存在：{sort}', code='SORT_INVALID')
    direction = 'DESC' if descending else 'ASC'
    if sort == 'occurred_at':
        order = (f'{temporal["kind"]} ASC, {temporal["whole"]} {direction} NULLS LAST, '
                 f'rtrim({temporal["fraction"]}, \'0\') {direction} NULLS LAST, '
                 f'CASE WHEN {temporal["kind"]} IN (2,3) THEN {quoted(sort)} END {direction} NULLS LAST')
    else:
        order = f'{quoted(sort)} {direction} NULLS LAST'
    if stable != sort:
        order += f', {quoted(stable)} ASC'
    # Index rows can contain both account roles for the same immutable record.
    # Include the role/version to make repeated page requests deterministic.
    if stable == 'record_id':
        for secondary in ('dataset_version', 'field'):
            if secondary in types and secondary != sort:
                order += f', {quoted(secondary)} ASC'
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    selected = ', '.join(quoted(column) for column in types) if temporal else '*'
    return f'SELECT {selected} FROM {source}{where}', values, order


def table_columns(description):
    return [{'key': col[0], 'label': LABELS.get(col[0], col[0]), 'type': str(col[1])} for col in description]


def paged(connection, sql, values, order, offset, limit):
    if offset < 0 or not 1 <= limit <= 2000:
        raise DataError('分页范围无效；每页应为 1 到 2000 条。', code='PAGING_INVALID')
    total = connection.execute(f'SELECT count(*) FROM ({sql}) counted', values).fetchone()[0]
    cursor = connection.execute(f'{sql} ORDER BY {order} LIMIT ? OFFSET ?', [*values, limit, offset])
    description = cursor.description
    columns = [col[0] for col in description]
    rows = [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]
    return {'columns': table_columns(description), 'rows': rows, 'total': total}


class QueryService:
    def __init__(self, project):
        self.project = project

    def page(self, object_id, filters=None, sort=None, descending=False, offset=0, limit=200):
        obj = self.project.get_object(object_id)
        with query_connection() as connection:
            description = register_object(connection, self.project.path, obj)
            sql, values, order = build_query(description, filters, sort, descending)
            result = paged(connection, sql, values, order, offset, limit)
            if sort == 'occurred_at' or any(item.get('column') == 'occurred_at' for item in filters or []):
                result['time_comparison'] = dict(TIME_COMPARISON)
            return result

    def sources(self, object_id, row_id, offset=0, limit=200):
        obj = self.project.get_object(object_id)
        with query_connection() as connection:
            if obj['kind'] == 'dataset':
                register_object(connection, self.project.path, obj)
                sql = ('SELECT record_id, ? AS dataset_version, source_id, source_sheet, source_row, '
                       'owner_account, counterparty_account, amount, currency, occurred_at '
                       'FROM records WHERE record_id=?')
                return paged(connection, sql, [obj['id'], row_id], 'source_row, record_id', offset, limit)
            path = obj['data'].get('lineage_path')
            if not path:
                raise DataError('该对象没有逐行来源关系。', code='NO_LINEAGE')
            connection.read_parquet(str(project_file(self.project.path, path))).create_view('lineage')
            sql = 'SELECT * FROM lineage WHERE result_row_id=?'
            return paged(connection, sql, [row_id], 'dataset_version, source_row, record_id', offset, limit)

    def source_preview(self, source_id, sheet, row, limit=15):
        source = self.project.get_object(source_id)
        if source['kind'] != 'source':
            raise DataError('来源对象类型不匹配。', code='SOURCE_INVALID')
        path = project_file(self.project.path, source['data']['stored_path'])
        row, limit = int(row), min(200, max(1, int(limit)))
        start = max(1, row - limit // 2)
        raw_rows, width = [], 0
        with WorkbookReader(path) as book:
            for number, cells in book.rows(sheet, start=start, end=start + limit - 1):
                width = max(width, max(cells, default=0))
                raw_rows.append((number, cells))
        columns = [{'key': 'row_number', 'label': '原始行号', 'type': 'INTEGER'}]
        columns.extend({'key': f'column_{i}', 'label': f'第 {i} 列', 'type': 'VARCHAR'} for i in range(1, width + 1))
        rows = []
        for number, cells in raw_rows:
            item = {'row_number': number}
            item.update({f'column_{i}': display_cell(cells.get(i)) for i in range(1, width + 1)})
            rows.append(item)
        return {'columns': columns, 'rows': rows, 'total': len(rows), 'highlight_row': row}

    def search_index(self, value, limit=200, offset=0):
        datasets = self.project.objects(kind='dataset')
        versions = {obj['id'] for obj in datasets if obj.get('state') in (None, 'ready', 'available', 'valid', 'committed')}
        indexes = self.project.objects(kind='index')
        if not indexes:
            return {'columns': [], 'rows': [], 'total': 0, 'stale': True, 'warning': '尚未建立项目索引，请先重建索引。'}
        current = [obj for obj in indexes if set(obj['data']['dataset_versions']) == versions
                   and obj.get('state') in (None, 'ready', 'available', 'valid', 'committed')]
        if not current:
            return {'columns': [], 'rows': [], 'total': 0, 'stale': True, 'warning': '数据版本已变化，索引过期，请重建。'}
        result = self.page(current[-1]['id'], [{'column': 'value', 'op': 'contains', 'value': value}],
                           sort='value', offset=offset, limit=limit)
        result.update(stale=False, warning='')
        return result
