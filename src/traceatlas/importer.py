"""Lossless, bounded-memory XLSX reading and the A-module import validation rules."""
from __future__ import annotations

import json
import posixpath
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format

from .contracts import TraceAtlasError

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
TEXT_FIELDS = (
    'owner_account', 'counterparty_account', 'owner_name', 'counterparty_name',
    'transaction_id', 'status', 'transaction_type', 'channel', 'ip', 'device_id', 'location',
)
IMPORT_FIELDS = (*TEXT_FIELDS, 'amount', 'balance', 'occurred_at', 'direction', 'currency')
MONEY_TYPE = 'decimal(38,12)'
MAX_REPORT_ITEMS = 200


class DataError(TraceAtlasError, ValueError):
    def __init__(self, message, code='DATA_INVALID', stage='validation', details=None, retryable=False):
        super().__init__(message, code=code, stage=stage, details=details, retryable=retryable)


class CancelledError(DataError):
    def __init__(self):
        super().__init__('任务已取消，未提交本次产物。', code='CANCELLED')


def check_cancelled(cancelled):
    if cancelled and cancelled():
        raise CancelledError()


def column_number(coordinate):
    result = 0
    for char in coordinate:
        if not char.isalpha():
            break
        result = result * 26 + ord(char.upper()) - 64
    return result


def _text(element):
    return ''.join(node.text or '' for node in element.iter(NS + 't'))


class WorkbookReader:
    """Streams worksheet XML; shared strings spill to SQLite rather than Python RAM.

    Values deliberately remain XML strings. In particular numeric account identifiers
    and decimals never pass through a binary float. The source file stays authoritative.
    """
    def __init__(self, path, cancelled=None, temporary_directory=None):
        self.path, self.cancelled = Path(path), cancelled
        self.temporary_directory = temporary_directory
        self.archive = None
        self._temporary = None
        self._strings = None
        self._lookup = None

    def __enter__(self):
        if self.path.suffix.lower() != '.xlsx':
            raise DataError('本轮只支持普通 .xlsx 工作簿。', code='UNSUPPORTED_FORMAT')
        try:
            self.archive = ZipFile(self.path)
            workbook = ET.fromstring(self.archive.read('xl/workbook.xml'))
            relationships = ET.fromstring(self.archive.read('xl/_rels/workbook.xml.rels'))
            paths = {rel.attrib['Id']: rel.attrib['Target'] for rel in relationships}
            self.sheets = {}
            for sheet in workbook.find(NS + 'sheets'):
                target = paths[sheet.attrib[REL_NS + 'id']]
                target = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
                if target not in self.archive.namelist():
                    raise DataError('工作表引用缺失，工作簿可能已损坏。', code='INVALID_WORKBOOK')
                self.sheets[sheet.attrib['name']] = target
            props = workbook.find(NS + 'workbookPr')
            self.date1904 = props is not None and props.attrib.get('date1904') in ('1', 'true')
            self.formats = ['General']
            if 'xl/styles.xml' in self.archive.namelist():
                style = ET.fromstring(self.archive.read('xl/styles.xml'))
                formats = dict(BUILTIN_FORMATS)
                for node in style.findall(NS + 'numFmts/' + NS + 'numFmt'):
                    formats[int(node.attrib['numFmtId'])] = node.attrib['formatCode']
                xfs = style.find(NS + 'cellXfs')
                if xfs is not None:
                    self.formats = [formats.get(int(x.attrib.get('numFmtId', '0')), 'General') for x in xfs]
            if 'xl/sharedStrings.xml' in self.archive.namelist():
                self._temporary = tempfile.TemporaryDirectory(prefix='traceatlas-strings-', dir=self.temporary_directory)
                self._strings = sqlite3.connect(str(Path(self._temporary.name) / 'strings.sqlite'))
                self._strings.execute('PRAGMA journal_mode=OFF')
                self._strings.execute('PRAGMA synchronous=OFF')
                self._strings.execute('CREATE TABLE strings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
                batch = []
                with self.archive.open('xl/sharedStrings.xml') as stream:
                    context = ET.iterparse(stream, events=('start', 'end'))
                    _, root = next(context)
                    index = 0
                    for event, elem in context:
                        if event == 'end' and elem.tag == NS + 'si':
                            batch.append((index, _text(elem)))
                            index += 1
                            elem.clear()
                            root.clear()
                            if len(batch) >= 4096:
                                check_cancelled(self.cancelled)
                                self._strings.executemany('INSERT INTO strings VALUES (?,?)', batch)
                                batch.clear()
                    self._strings.executemany('INSERT INTO strings VALUES (?,?)', batch)
                self._strings.commit()
                self._lookup = lru_cache(maxsize=8192)(self._get_string)
            return self
        except (OSError, BadZipFile, ET.ParseError, KeyError) as exc:
            self.close()
            raise DataError(f'无法读取工作簿：{exc}。请检查文件是否损坏、加密或被占用。',
                            code='INVALID_WORKBOOK') from exc
        except BaseException:
            self.close()
            raise

    def _get_string(self, index):
        value = self._strings.execute('SELECT value FROM strings WHERE id=?', (index,)).fetchone()
        if value is None:
            raise DataError(f'工作簿共享文本编号 {index} 无效。', code='INVALID_WORKBOOK')
        return value[0]

    def close(self):
        if self._lookup:
            self._lookup.cache_clear()
        if self._strings:
            self._strings.close()
            self._strings = None
        if self._temporary:
            self._temporary.cleanup()
            self._temporary = None
        if self.archive:
            self.archive.close()
            self.archive = None

    def __exit__(self, *args):
        self.close()

    def rows(self, sheet, start=1, end=None):
        if sheet not in self.sheets:
            raise DataError(f'工作表不存在：{sheet}', code='SHEET_MISSING')
        with self.archive.open(self.sheets[sheet]) as stream:
            context = ET.iterparse(stream, events=('start', 'end'))
            sheet_data = None
            implicit_row = 0
            for event, elem in context:
                if event == 'start' and elem.tag == NS + 'sheetData':
                    sheet_data = elem
                if event != 'end' or elem.tag != NS + 'row':
                    continue
                implicit_row = int(elem.attrib.get('r', implicit_row + 1))
                if end is not None and implicit_row > end:
                    break
                check_cancelled(self.cancelled)
                if implicit_row >= start:
                    cells = {}
                    previous_col = 0
                    for node in elem.findall(NS + 'c'):
                        coord = node.attrib.get('r', '')
                        col = column_number(coord) or previous_col + 1
                        previous_col = col
                        kind = node.attrib.get('t', 'n')
                        value_node = node.find(NS + 'v')
                        raw = value_node.text if value_node is not None else None
                        formula = node.find(NS + 'f')
                        inline = node.find(NS + 'is')
                        if kind == 's' and raw is not None:
                            if self._lookup is None:
                                raise DataError('工作簿缺少共享文本表。', code='INVALID_WORKBOOK')
                            value = self._lookup(int(raw))
                        elif kind == 'inlineStr':
                            value = _text(inline) if inline is not None else ''
                        else:
                            value = raw
                        style = int(node.attrib.get('s', '0'))
                        fmt = self.formats[style] if style < len(self.formats) else 'General'
                        cells[col] = {'value': value, 'raw': raw, 'type': kind,
                                      'formula': None if formula is None else '=' + (formula.text or ''),
                                      'formula_attributes': None if formula is None else dict(formula.attrib),
                                      'format': fmt, 'coordinate': coord}
                    yield implicit_row, cells
                elem.clear()
                if sheet_data is not None:
                    sheet_data.clear()

    def estimated_rows(self, sheet):
        """Dimension is only a hint, never the authority for iteration or row counts."""
        with self.archive.open(self.sheets[sheet]) as stream:
            for event, elem in ET.iterparse(stream, events=('start',)):
                if elem.tag == NS + 'dimension':
                    numbers = re.findall(r'\d+', elem.attrib.get('ref', ''))
                    return int(numbers[-1]) if numbers else 0
                if elem.tag == NS + 'sheetData':
                    return 0
        return 0


def display_cell(cell):
    if not cell:
        return ''
    value = cell['value']
    if value is None and cell.get('formula'):
        return cell['formula'] + ' [无缓存结果]'
    return '' if value is None else str(value)


def inspect_workbook(path, sheet=None, header_row=1, limit=20):
    if header_row < 1 or limit < 1 or limit > 200:
        raise DataError('表头行和预览行数超出范围。')
    with WorkbookReader(path) as book:
        if not book.sheets:
            raise DataError('工作簿没有可读取的工作表。')
        sheet = sheet or next(iter(book.sheets))
        if sheet not in book.sheets:
            raise DataError(f'工作表不存在：{sheet}')
        header, rows, row_numbers = {}, [], []
        width = 0
        for row_number, cells in book.rows(sheet, start=header_row):
            width = max(width, max(cells, default=0))
            if row_number == header_row:
                header = cells
            else:
                rows.append(cells)
                row_numbers.append(row_number)
                if len(rows) >= limit:
                    break
        headers = [display_cell(header.get(i)) or f'第 {i} 列（空表头）' for i in range(1, width + 1)]
        warnings = []
        duplicates = [name for name, count in Counter(headers).items() if count > 1]
        if duplicates:
            warnings.append('存在重复表头，请按列位置区分：' + '、'.join(duplicates))
        return {'sheets': list(book.sheets), 'sheet': sheet, 'headers': headers,
                'rows': [[display_cell(cells.get(i)) for i in range(1, width + 1)] for cells in rows],
                'row_numbers': row_numbers, 'estimated_rows': max(0, book.estimated_rows(sheet) - header_row),
                'warnings': warnings}


def parse_money(value):
    if value is None or value == '':
        raise ValueError('金额为空')
    text = str(value)
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', text.strip()):
        raise ValueError('金额不是有效十进制数；本轮不自动移除货币符号或千分位')
    try:
        number = Decimal(text)
        if not number.is_finite():
            raise ValueError('金额必须是有限数值')
        exponent = number.as_tuple().exponent
        decimal_places = max(0, -exponent)
        integer_places = max(0, number.adjusted() + 1) if number else 0
        if decimal_places > 12 or integer_places > 26:
            raise ValueError('金额超出 26 位整数、12 位小数范围；未进行舍入')
        with localcontext() as ctx:
            ctx.prec = 50
            return number.quantize(Decimal('0.000000000001'))
    except InvalidOperation as exc:
        raise ValueError('金额不是有效十进制数') from exc


def parse_time(cell, date1904=False):
    value = cell.get('value') if cell else None
    if value is None or value == '':
        return None, 'missing', None
    text = str(value)
    fmt = cell.get('format', 'General')
    if cell.get('type') == 'n' and is_date_format(fmt):
        serial = Decimal(text)
        days = int(serial // 1)
        if not date1904 and days == 60:
            raise ValueError('Excel 序号 60 对应不存在的 1900-02-29')
        epoch = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
        if not date1904 and 0 < serial < 60:
            days += 1
        micros = int(((serial % 1) * Decimal(86400000000)).to_integral_value())
        value_dt = epoch + timedelta(days=days, microseconds=micros)
        # Excel display formatting is evidence of the source time precision.
        clean_fmt = re.sub(r'"[^"]*"|\\.', '', fmt).lower()
        if not re.search(r'[hs]', clean_fmt):
            return value_dt.date().isoformat(), 'day', None
        precision = 'microsecond' if value_dt.microsecond else ('second' if 's' in clean_fmt else 'minute')
        return value_dt.isoformat(timespec='microseconds' if value_dt.microsecond else 'seconds'), precision, None
    normalized = re.sub(r'^(\d{4})/(\d{1,2})/(\d{1,2})', r'\1-\2-\3', text)
    # Only year-first formats are accepted; 03/04/2026 remains an explicit error.
    match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', normalized)
    if match:
        return date(*map(int, match.groups())).isoformat(), 'day', None
    if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', normalized):
        raise ValueError('时间格式不明确，请使用年-月-日或年-月-日 时:分:秒')
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError('无效日期或时间') from exc
    fractional = re.search(r'\d{2}:\d{2}:\d{2}[.,](\d+)', normalized)
    if fractional:
        digits = len(fractional.group(1))
        precision = {3: 'millisecond', 6: 'microsecond', 9: 'nanosecond'}.get(digits, f'fractional_second_{digits}')
    else:
        precision = 'second' if re.search(r'\d{2}:\d{2}:\d{2}', normalized) else 'minute'
    tz = result.strftime('%z') if result.tzinfo else None
    # datetime validates the calendar, but its microsecond storage must not round/truncate
    # the original fractional precision or manufacture seconds for minute-only inputs.
    return normalized.replace(' ', 'T', 1).replace(',', '.'), precision, tz


def validate_parameters(parameters):
    mapping = parameters.get('mapping', {})
    if not mapping.get('owner_account') or not mapping.get('amount'):
        raise DataError('必须映射本方账户和交易金额。', code='MAPPING_REQUIRED')
    if any(key not in IMPORT_FIELDS for key in mapping):
        raise DataError('映射包含不支持的标准字段。', code='MAPPING_INVALID')
    values = list(mapping.values())
    if any(not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16384 for value in values):
        raise DataError('映射列号必须是 1 到 16384 的整数。', code='MAPPING_INVALID')
    if len(values) != len(set(values)):
        raise DataError('同一个源列不能重复映射；请检查字段选择。', code='MAPPING_INVALID')
    header = int(parameters.get('header_row', 1))
    start = int(parameters.get('start_row') or header + 1)
    end = parameters.get('end_row')
    end = int(end) if end else None
    if header < 1 or start <= header or (end is not None and end < start):
        raise DataError('数据范围必须在表头之后，结束行不能早于开始行。', code='RANGE_INVALID')
    return mapping, start, end


def normalize_row(cells, mapping, parameters, date1904=False):
    record, errors, warnings = {}, [], []
    normalized = {}
    mapped_columns = set(mapping.values())
    for col, cell in cells.items():
        if col not in mapped_columns and cell.get('formula') and cell.get('value') is None:
            warnings.append({'field': f'column_{col}', 'message': '未映射列的公式没有缓存结果；已保留公式原文'})
    for field, col in mapping.items():
        cell = cells.get(col)
        value = cell.get('value') if cell else None
        if cell and cell.get('formula') and value is None:
            issue = {'field': field, 'message': '公式没有已保存的计算结果，请在 Excel 中计算并保存后再导入'}
            (errors if field in ('owner_account', 'amount') else warnings).append(issue)
        if cell and cell.get('type') == 'e':
            errors.append({'field': field, 'message': f'Excel 单元格错误：{value}'})
        normalized[field] = value
    for field in TEXT_FIELDS:
        record[field] = normalized.get(field)
    if record['owner_account'] is None or record['owner_account'] == '':
        errors.append({'field': 'owner_account', 'message': '本方账户为空'})
    for field in ('owner_account', 'counterparty_account', 'transaction_id', 'device_id'):
        cell = cells.get(mapping.get(field))
        if cell and cell['type'] == 'n' and cell['value'] is not None:
            raw = cell['value']
            try:
                number = Decimal(raw)
                record[field] = format(number, 'f')
                if len(number.as_tuple().digits) > 15 or number.adjusted() >= 15:
                    warnings.append({'field': field, 'message': '长编号在源 Excel 中是数值；可能已被 Excel 截断，无法恢复缺失位'})
                if (re.fullmatch(r'0+', cell.get('format', ''))
                        and number == number.to_integral_value() and number >= 0):
                    record[field] = record[field].split('.')[0].zfill(len(cell['format']))
            except InvalidOperation:
                errors.append({'field': field, 'message': '编号单元格数值无效'})
    for field in ('amount', 'balance'):
        value = normalized.get(field)
        record[field + '_raw'] = value
        record[field] = None
        if field == 'balance' and value in (None, ''):
            continue
        try:
            record[field] = parse_money(value)
        except ValueError as exc:
            errors.append({'field': field, 'message': str(exc)})
    time_cell = cells.get(mapping.get('occurred_at'))
    try:
        occurred, precision, source_tz = parse_time(time_cell, date1904)
    except (ValueError, InvalidOperation, OverflowError) as exc:
        errors.append({'field': 'occurred_at', 'message': str(exc)})
        occurred, precision, source_tz = None, 'invalid', None
    record['occurred_at'], record['time_precision'] = occurred, precision
    record['timezone'] = source_tz or parameters.get('timezone') or 'unknown'
    if precision == 'missing':
        warnings.append({'field': 'occurred_at', 'message': '缺少交易时间；不能用于要求准确时间的分析'})
    record['currency'] = normalized.get('currency') or parameters.get('currency') or 'unknown'
    record['platform'] = parameters.get('platform') or 'unknown'
    record['account_namespace'] = parameters.get('account_namespace') or 'unknown'
    raw_direction = normalized.get('direction')
    record['direction_raw'] = raw_direction
    # Bank-specific borrow/lend flags are deliberately not guessed.
    record['direction'] = raw_direction if raw_direction in ('in', 'out') else 'unknown'
    if raw_direction not in (None, '', 'in', 'out', 'unknown'):
        warnings.append({'field': 'direction', 'message': '原方向未映射为 in/out，保留为未知'})
    if record['direction'] == 'in' and record['amount'] is not None and record['amount'] < 0:
        warnings.append({'field': 'direction', 'message': '收入方向与负金额冲突，保留原值'})
    if record['counterparty_account'] in (None, ''):
        warnings.append({'field': 'counterparty_account', 'message': '缺少对方账户'})
    record['quality_flags'] = json.dumps(warnings, ensure_ascii=False, separators=(',', ':'))
    record['raw_cells'] = json.dumps(cells, ensure_ascii=False, separators=(',', ':'))
    return record, errors, warnings


@contextmanager
def import_rows(parameters, cancelled=None):
    mapping, start, end = validate_parameters(parameters)
    with WorkbookReader(parameters['path'], cancelled=cancelled,
                        temporary_directory=parameters.get('_temporary_directory')) as book:
        sheet = parameters.get('sheet') or next(iter(book.sheets))
        if sheet not in book.sheets:
            raise DataError(f'工作表不存在：{sheet}', code='SHEET_MISSING')
        yield book, sheet, book.rows(sheet, start=start, end=end), mapping


def validate_import(parameters, cancelled=None):
    report = {'input_count': 0, 'valid_count': 0, 'rejected_count': 0, 'blank_count': 0,
              'errors': [], 'warnings': [], 'error_count': 0, 'warning_count': 0}
    with import_rows(parameters, cancelled) as (book, sheet, rows, mapping):
        for row, cells in rows:
            report['input_count'] += 1
            if not any(cell['value'] is not None or cell.get('formula') for cell in cells.values()):
                report['blank_count'] += 1
                continue
            _record, errors, warnings = normalize_row(cells, mapping, parameters, book.date1904)
            report['rejected_count' if errors else 'valid_count'] += 1
            for key, issues in (('errors', errors), ('warnings', warnings)):
                report['error_count' if key == 'errors' else 'warning_count'] += len(issues)
                remaining = MAX_REPORT_ITEMS - len(report[key])
                report[key].extend({'row': row, 'sheet': sheet, **issue} for issue in issues[:max(0, remaining)])
    report['errors_truncated'] = report['error_count'] > len(report['errors'])
    report['warnings_truncated'] = report['warning_count'] > len(report['warnings'])
    return report
