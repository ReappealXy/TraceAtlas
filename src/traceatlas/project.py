"""Project persistence, single-writer coordination and recoverable artifact commits.

Only the application service uses this module to publish worker output. Workers
write staging files and never write the authoritative database themselves.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .contracts import ACTIVE_STATES, PROJECT_SCHEMA_VERSION, TraceAtlasError, contained_path, new_id, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'ready', data TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS objects_kind_state ON objects(kind,state);
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, state TEXT NOT NULL, current_attempt_id TEXT NOT NULL,
 spec TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
 attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
 state TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_task ON attempts(task_id);
CREATE TABLE IF NOT EXISTS commits (
 attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, state TEXT NOT NULL,
 manifest TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS committed_task ON commits(task_id) WHERE state='committed';
CREATE TABLE IF NOT EXISTS artifacts (
 path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
 attempt_id TEXT NOT NULL, object_ids TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + new_id() + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _WriteLock:
    def __init__(self, root):
        self.stream = None
        try:
            stream = (root / '.write.lock').open('a+b')
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.stream = stream
        except OSError as exc:
            if 'stream' in locals():
                stream.close()
            raise TraceAtlasError('项目正被另一个窗口使用，或目录不可写。可选择只读打开；请勿删除锁文件。',
                                  'PROJECT_LOCKED', 'open', details={'reason': str(exc)}) from exc

    def close(self):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


class Project:
    def __init__(self, path, manifest, connection, readonly=False, write_lock=None):
        self.path = Path(path).resolve()
        self.manifest = manifest
        self.id = manifest['project_id']
        self.name = manifest['name']
        self.readonly = readonly
        self.connection = connection
        self._write_lock = write_lock
        self._mutex = threading.RLock()
        self._commit_mutex = threading.RLock()
        self._closed = False
        self.recovery_report = []

    @classmethod
    def create(cls, path, name):
        path = Path(path).resolve()
        if not str(name).strip():
            raise TraceAtlasError('请填写项目名称。', 'INVALID_PROJECT_NAME', 'create')
        if path.exists():
            raise TraceAtlasError('目标目录已存在，请选择一个新的项目目录。', 'PROJECT_EXISTS', 'create')
        created = False
        try:
            path.mkdir(parents=True, exist_ok=False)
            created = True
            for folder in ('sources', 'datasets', 'results', 'indexes', 'staging', 'exports', 'logs'):
                (path / folder).mkdir()
            manifest = {'application': 'TraceAtlas', 'project_id': new_id(), 'name': str(name).strip(),
                        'schema_version': PROJECT_SCHEMA_VERSION, 'created_at': utc_now(),
                        'metadata_path': 'metadata.sqlite'}
            connection = sqlite3.connect(path / 'metadata.sqlite')
            try:
                connection.executescript(SCHEMA)
                connection.execute(f'PRAGMA user_version={PROJECT_SCHEMA_VERSION}')
                connection.commit()
            finally:
                connection.close()
            _write_json(path / 'project.json', manifest)
            return cls.open(path)
        except Exception as exc:
            if created:
                shutil.rmtree(path, ignore_errors=True)
            if isinstance(exc, TraceAtlasError):
                raise
            raise TraceAtlasError('无法创建项目，请检查保存位置权限和可用磁盘空间。',
                                  'PROJECT_CREATE_FAILED', 'create', details={'reason': str(exc)}) from exc

    @classmethod
    def open(cls, path, readonly=False, migrate=False):
        path = Path(path).resolve()
        if path.is_file() and path.name == 'project.json':
            path = path.parent
        lock = None
        connection = None
        try:
            if not (path / 'project.json').is_file():
                raise TraceAtlasError('该目录没有 TraceAtlas 项目说明文件。', 'PROJECT_NOT_FOUND', 'open')
            manifest = cls._read_manifest(path)
            if manifest['schema_version'] > PROJECT_SCHEMA_VERSION:
                raise TraceAtlasError('此项目由更新版本创建，请使用相同或更新版本的 TraceAtlas。',
                                      'PROJECT_TOO_NEW', 'open')
            if not readonly:
                lock = _WriteLock(path)
                cls._recover_migration(path)
                manifest = cls._read_manifest(path)
            elif (path / '.migration.json').exists():
                raise TraceAtlasError('项目升级尚未恢复，请先使用可写模式打开以恢复。', 'MIGRATION_RECOVERY_REQUIRED', 'open')
            database = contained_path(path, manifest.get('metadata_path', 'metadata.sqlite'))
            if not database.is_file():
                raise TraceAtlasError('项目元数据库缺失，请从完整备份恢复。', 'DATABASE_MISSING', 'open')
            if manifest['schema_version'] < PROJECT_SCHEMA_VERSION:
                if not migrate or readonly:
                    raise TraceAtlasError('项目需要先备份并升级格式后才能打开。原文件不会直接被覆盖。',
                                          'MIGRATION_REQUIRED', 'open', details={'version': manifest['schema_version']})
                cls._migrate(path, manifest)
                manifest = cls._read_manifest(path)
            connection = sqlite3.connect(database.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                                         uri=True, timeout=10, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise TraceAtlasError('项目数据库完整性检查失败，请保留项目并从备份恢复。', 'DATABASE_CORRUPT', 'open')
            database_version = connection.execute('PRAGMA user_version').fetchone()[0]
            if database_version != manifest['schema_version']:
                raise TraceAtlasError('项目说明与数据库格式版本不一致，请从备份恢复。', 'VERSION_MISMATCH', 'open')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=10000')
            if not readonly:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA synchronous=FULL')
            project = cls(path, manifest, connection, readonly, lock)
            if not readonly:
                project._recover()
            else:
                project._check_artifacts(update=False)
            return project
        except BaseException as exc:
            if connection:
                connection.close()
            if lock:
                lock.close()
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, TraceAtlasError):
                raise
            raise TraceAtlasError('无法打开项目。请检查目录权限、磁盘空间或数据库完整性；不会自动创建空项目。',
                                  'PROJECT_OPEN_FAILED', 'open', details={'reason': str(exc)}) from exc

    @staticmethod
    def _read_manifest(path):
        try:
            manifest = json.loads((path / 'project.json').read_text(encoding='utf-8-sig'))
            if (manifest.get('application') != 'TraceAtlas' or not manifest.get('project_id')
                    or not manifest.get('name') or not isinstance(manifest.get('schema_version'), int)):
                raise ValueError('invalid project manifest')
            return manifest
        except (OSError, ValueError, TypeError) as exc:
            raise TraceAtlasError('项目说明文件损坏或不是 TraceAtlas 项目。请保留目录并检查备份。',
                                  'INVALID_PROJECT', 'open') from exc

    def _require_write(self):
        if self._closed:
            raise TraceAtlasError('项目已经关闭。', 'PROJECT_CLOSED')
        if self.readonly:
            raise TraceAtlasError('项目处于只读模式，请关闭占用它的窗口后重新打开。', 'READ_ONLY')

    @contextmanager
    def _transaction(self):
        self._require_write()
        with self._mutex:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield self.connection
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def close(self):
        with self._mutex:
            if not self._closed:
                if not self.readonly:
                    try:
                        self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    except sqlite3.Error:
                        pass
                self.connection.close()
                if self._write_lock:
                    self._write_lock.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def save_session(self, data):
        with self._transaction() as connection:
            connection.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                               ('session', _json(data)))

    def load_session(self):
        with self._mutex:
            row = self.connection.execute("SELECT value FROM settings WHERE key='session'").fetchone()
            return json.loads(row[0]) if row else {}

    @staticmethod
    def _object(row):
        result = dict(row)
        result['data'] = json.loads(result['data'])
        return result

    def objects(self, kind=None, include_deleted=False):
        where, args = [], []
        if kind is not None:
            where.append('kind=?')
            args.append(kind)
        if not include_deleted:
            where.append("state<>'deleted'")
        sql = 'SELECT * FROM objects' + (' WHERE ' + ' AND '.join(where) if where else '')
        with self._mutex:
            objects = [self._object(row) for row in self.connection.execute(sql + ' ORDER BY created_at,id', args)]
        if self.readonly:
            for obj in objects:
                if obj['id'] in getattr(self, '_readonly_damaged', set()):
                    obj['state'] = 'source_missing' if obj['kind'] == 'source' else 'damaged'
        return objects

    def get_object(self, object_id):
        with self._mutex:
            row = self.connection.execute('SELECT * FROM objects WHERE id=?', (object_id,)).fetchone()
        if row is None:
            raise TraceAtlasError('对象不存在或未正式提交。', 'OBJECT_NOT_FOUND', details={'id': object_id})
        obj = self._object(row)
        if self.readonly and object_id in getattr(self, '_readonly_damaged', set()):
            obj['state'] = 'source_missing' if obj['kind'] == 'source' else 'damaged'
        return obj

    def _insert_object(self, connection, obj):
        now = utc_now()
        connection.execute('INSERT INTO objects VALUES (?,?,?,?,?,?,?)',
                           (obj['id'], obj['kind'], obj['name'], obj.get('state', 'ready'),
                            _json(obj.get('data', {})), obj.get('created_at', now), now))

    def put_object(self, obj):
        """Register metadata-only objects; immutable data files use commit()."""
        with self._transaction() as connection:
            self._insert_object(connection, obj)
        return self.get_object(obj['id'])

    def delete_object(self, object_id):
        target = self.get_object(object_id)
        if target['kind'] in {'source', 'batch'}:
            raise TraceAtlasError('原文件和导入批次保留为审计依据，不可单独删除。', 'SOURCE_PRESERVED')
        if target['state'] == 'deleted':
            return
        for obj in self.objects():
            if obj['id'] == object_id or obj['kind'] in {'batch', 'index'}:
                continue
            data = obj['data']
            refs = data.get('dataset_versions', []) + data.get('parents', [])
            refs += [data.get('source_id'), data.get('object_id')]
            if object_id in refs:
                raise TraceAtlasError('该对象仍被其他已保存结果引用，请先处理下游结果。',
                                      'OBJECT_REFERENCED', details={'dependent_id': obj['id'], 'dependent_name': obj['name']})
        with self._transaction() as connection:
            connection.execute("UPDATE objects SET state='deleted',updated_at=? WHERE id=?", (utc_now(), object_id))
            if target['kind'] == 'dataset':
                self._invalidate_indexes(connection)

    def _invalidate_indexes(self, connection):
        live = {row[0] for row in connection.execute("SELECT id FROM objects WHERE kind='dataset' AND state='ready'")}
        for row in connection.execute("SELECT id,data FROM objects WHERE kind='index' AND state IN ('ready','stale')").fetchall():
            state = 'ready' if set(json.loads(row['data']).get('dataset_versions', [])) == live else 'stale'
            connection.execute('UPDATE objects SET state=?,updated_at=? WHERE id=?', (state, utc_now(), row['id']))

    def create_task(self, spec):
        spec = dict(spec)
        task_id, attempt_id, now = spec.setdefault('task_id', new_id()), spec.setdefault('attempt_id', new_id()), utc_now()
        spec.setdefault('project_id', self.id)
        spec.setdefault('created_at', now)
        if spec['project_id'] != self.id:
            raise TraceAtlasError('任务不属于当前项目。', 'PROJECT_MISMATCH')
        with self._transaction() as connection:
            connection.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',
                               (task_id, 'queued', attempt_id, _json(spec), '{}', now, now))
            connection.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                               (attempt_id, task_id, 'queued', '{}', now, now))
        return self.get_task(task_id)

    @staticmethod
    def _task(row):
        result = json.loads(row['spec'])
        result.update(json.loads(row['data']))
        result.update({key: row[key] for key in ('task_id', 'state', 'current_attempt_id', 'created_at', 'updated_at')})
        result['attempt_id'] = row['current_attempt_id']
        return result

    def get_task(self, task_id):
        with self._mutex:
            row = self.connection.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise TraceAtlasError('任务不存在。', 'TASK_NOT_FOUND')
        return self._task(row)

    def list_tasks(self):
        with self._mutex:
            return [self._task(row) for row in self.connection.execute('SELECT * FROM tasks ORDER BY created_at,task_id')]

    def update_task(self, task_id, state=None, **fields):
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise TraceAtlasError('任务不存在。', 'TASK_NOT_FOUND')
            if row['state'] == 'succeeded' and state not in (None, 'succeeded'):
                raise TraceAtlasError('已成功提交的任务不能改成其他状态。', 'TASK_ALREADY_COMMITTED')
            data = json.loads(row['data'])
            data.update(fields)
            status = state or row['state']
            if status not in ACTIVE_STATES | {'succeeded', 'failed', 'cancelled', 'interrupted'}:
                raise TraceAtlasError('不受支持的任务状态。', 'INVALID_TASK_STATE')
            connection.execute('UPDATE tasks SET state=?,data=?,updated_at=? WHERE task_id=?',
                               (status, _json(data), utc_now(), task_id))
            attempt = connection.execute('SELECT data FROM attempts WHERE attempt_id=?', (row['current_attempt_id'],)).fetchone()
            attempt_data = json.loads(attempt['data'])
            attempt_data.update(fields)
            connection.execute('UPDATE attempts SET state=?,data=?,updated_at=? WHERE attempt_id=?',
                               (status, _json(attempt_data), utc_now(), row['current_attempt_id']))
        return self.get_task(task_id)

    def create_attempt(self, task_id, attempt_id=None):
        attempt_id, now = attempt_id or new_id(), utc_now()
        with self._transaction() as connection:
            task = connection.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if task is None:
                raise TraceAtlasError('任务不存在。', 'TASK_NOT_FOUND')
            if task['state'] not in {'failed', 'cancelled', 'interrupted'}:
                raise TraceAtlasError('只有失败、取消或中断的任务可以重试。', 'TASK_NOT_RETRYABLE')
            if connection.execute("SELECT 1 FROM commits WHERE task_id=? AND state='committed'", (task_id,)).fetchone():
                raise TraceAtlasError('该任务已有正式结果，不能重复提交。', 'TASK_ALREADY_COMMITTED')
            connection.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)', (attempt_id, task_id, 'queued', '{}', now, now))
            connection.execute("UPDATE tasks SET state='queued',current_attempt_id=?,data='{}',updated_at=? WHERE task_id=?",
                               (attempt_id, now, task_id))
        return self.get_attempt(attempt_id)

    @staticmethod
    def _attempt(row):
        result = dict(row)
        result.update(json.loads(result.pop('data')))
        return result

    def get_attempt(self, attempt_id):
        with self._mutex:
            row = self.connection.execute('SELECT * FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if row is None:
            raise TraceAtlasError('任务尝试不存在。', 'ATTEMPT_NOT_FOUND')
        return self._attempt(row)

    def task_attempts(self, task_id):
        with self._mutex:
            return [self._attempt(row) for row in self.connection.execute('SELECT * FROM attempts WHERE task_id=? ORDER BY created_at', (task_id,))]

    def update_attempt(self, attempt_id, state=None, **fields):
        attempt = self.get_attempt(attempt_id)
        task = self.get_task(attempt['task_id'])
        if task['current_attempt_id'] == attempt_id:
            self.update_task(attempt['task_id'], state, **fields)
        else:
            raise TraceAtlasError('不能更新过期的任务尝试。', 'STALE_ATTEMPT')
        return self.get_attempt(attempt_id)

    def _validate_commit(self, manifest, staging):
        files, exports, objects = manifest.get('files', []), manifest.get('exports', []), manifest.get('objects', [])
        destinations, object_ids = set(), set()
        for item in [*files, *exports]:
            source = contained_path(staging, item['source'])
            if not source.is_file() or source.stat().st_size != item['size'] or file_hash(source) != item['sha256']:
                raise TraceAtlasError('临时产物不完整或校验值不符，未提交任何结果。', 'ARTIFACT_MISMATCH', 'verifying')
            dest = contained_path(self.path, item['destination']) if item in files else Path(item['destination']).resolve()
            if item in files and Path(item['destination']).parts[0] not in {'sources', 'datasets', 'results', 'indexes', 'exports'}:
                raise TraceAtlasError('产物不能覆盖项目管理文件。', 'INVALID_DESTINATION', 'verifying')
            canonical = os.path.normcase(str(dest))
            if canonical in destinations:
                raise TraceAtlasError('产物目标路径重复。', 'DUPLICATE_DESTINATION', 'verifying')
            destinations.add(canonical)
            if dest.exists() and (item in files or not item.get('overwrite', False)):
                raise TraceAtlasError('输出文件已经存在，请选择其他文件名或明确允许覆盖。', 'FILE_EXISTS', 'verifying')
            if item in exports:
                if not dest.parent.is_dir() or dest.is_dir():
                    raise TraceAtlasError('导出目录不存在或目标不是普通文件。', 'EXPORT_DIRECTORY', 'verifying')
                if dest.is_relative_to(self.path) and not dest.is_relative_to(self.path / 'exports'):
                    raise TraceAtlasError('请导出到项目的 exports 目录或项目之外，避免覆盖内部数据。', 'INVALID_EXPORT_DESTINATION')
        new_objects = {obj['id']: obj for obj in objects}
        if len(new_objects) != len(objects):
            raise TraceAtlasError('提交对象标识重复。', 'DUPLICATE_OBJECT', 'verifying')
        paths = {item['destination']: contained_path(staging, item['source']) for item in files}
        existing = {obj['id']: obj for obj in self.objects()}
        for obj in objects:
            if not obj.get('id') or obj['id'] in existing or not obj.get('kind') or not obj.get('name'):
                raise TraceAtlasError('提交对象缺少标识或与已有对象冲突。', 'INVALID_OBJECT', 'verifying')
            object_ids.add(obj['id'])
            data = obj.get('data', {})
            for key in ('stored_path', 'data_path', 'lineage_path', 'errors_path', 'quality_path'):
                if data.get(key) and data[key] not in paths:
                    raise TraceAtlasError('对象引用了未登记的产物。', 'UNREGISTERED_ARTIFACT', 'verifying')
            refs = data.get('dataset_versions', []) + data.get('parents', []) + data.get('source_ids', [])
            refs += [data[k] for k in ('source_id', 'batch_id', 'object_id') if data.get(k)]
            for ref in refs:
                if ref not in new_objects and (ref not in existing or existing[ref]['state'] != 'ready'):
                    raise TraceAtlasError('对象的输入或来源不存在或不可用。', 'MISSING_REFERENCE', 'verifying', details={'reference': ref})
            if data.get('data_path', '').endswith('.parquet'):
                import pyarrow.parquet as pq
                parquet = pq.ParquetFile(paths[data['data_path']])
                if parquet.metadata.num_rows != data.get('row_count'):
                    raise TraceAtlasError('数据文件行数与登记行数不一致。', 'ROW_COUNT_MISMATCH', 'verifying')
                required = {'record_id', 'source_id', 'source_sheet', 'source_row'} if obj['kind'] == 'dataset' else set()
                if not required.issubset(parquet.schema_arrow.names):
                    raise TraceAtlasError('标准数据缺少必要的来源字段。', 'MISSING_PROVENANCE', 'verifying')
            if data.get('lineage_path'):
                import pyarrow.parquet as pq
                lineage = pq.ParquetFile(paths[data['lineage_path']])
                if lineage.metadata.num_rows != data.get('metrics', {}).get('contributing_count'):
                    raise TraceAtlasError('结果来源行数与参与记录数不一致。', 'LINEAGE_COUNT_MISMATCH', 'verifying')
            if obj['kind'] == 'source' and data.get('content_hash') != file_hash(paths[data['stored_path']]):
                raise TraceAtlasError('来源内容摘要与原文件不一致。', 'SOURCE_HASH_MISMATCH', 'verifying')
        self._validate_record_lineage(objects, paths, existing, staging)

    def _validate_record_lineage(self, objects, paths, existing, staging):
        parquet_objects = [obj for obj in objects if obj['data'].get('data_path', '').endswith('.parquet')]
        if not parquet_objects:
            return
        import duckdb
        connection = duckdb.connect()
        try:
            connection.execute("SET memory_limit='256MB'")
            connection.execute('SET threads=2')
            connection.execute('SET temp_directory=?', [str(staging / '.validation-spill')])
            new_objects = {obj['id']: obj for obj in objects}
            for obj in parquet_objects:
                data = obj['data']
                connection.read_parquet(str(paths[data['data_path']])).create_view('validated', replace=True)
                if obj['kind'] == 'dataset':
                    bad = connection.execute('''SELECT count(*) FROM validated WHERE record_id IS NULL
                        OR source_id IS NULL OR source_sheet IS NULL OR source_row IS NULL OR source_row<1
                        OR source_id<>? OR source_sheet<>?''', [data['source_id'], data['sheet']]).fetchone()[0]
                    distinct = connection.execute('SELECT count(DISTINCT record_id) FROM validated').fetchone()[0]
                    if bad or distinct != data['row_count']:
                        raise TraceAtlasError('标准记录标识重复或来源行定位不完整。', 'INVALID_RECORD_PROVENANCE', 'verifying')
                elif obj['kind'] == 'result' and data.get('lineage_path'):
                    distinct = connection.execute('SELECT count(DISTINCT result_row_id) FROM validated').fetchone()[0]
                    if distinct != data['row_count']:
                        raise TraceAtlasError('结果行标识缺失或重复。', 'INVALID_RESULT_IDS', 'verifying')
                    connection.read_parquet(str(paths[data['lineage_path']])).create_view('validated_lineage', replace=True)
                    unions = []
                    for number, version in enumerate(data.get('dataset_versions', [])):
                        source_obj = new_objects.get(version) or existing[version]
                        source_path = paths.get(source_obj['data']['data_path']) or contained_path(self.path, source_obj['data']['data_path'])
                        connection.read_parquet(str(source_path)).create_view(f'input_{number}', replace=True)
                        escaped = version.replace("'", "''")
                        unions.append(f"SELECT record_id, source_id, source_sheet, source_row, '{escaped}' AS dataset_version FROM input_{number}")
                    if not unions and data['row_count']:
                        raise TraceAtlasError('结果没有固定的输入版本。', 'MISSING_REFERENCE', 'verifying')
                    if unions:
                        connection.execute('CREATE OR REPLACE VIEW validated_inputs AS ' + ' UNION ALL '.join(unions))
                        bad = connection.execute('''SELECT count(*) FROM validated_lineage l
                            LEFT JOIN validated r ON l.result_row_id=r.result_row_id
                            LEFT JOIN validated_inputs i ON l.record_id=i.record_id AND l.dataset_version=i.dataset_version
                                AND l.source_id=i.source_id AND l.source_sheet=i.source_sheet AND l.source_row=i.source_row
                            WHERE r.result_row_id IS NULL OR i.record_id IS NULL''').fetchone()[0]
                        untraced = connection.execute('''SELECT count(*) FROM validated r WHERE NOT EXISTS
                            (SELECT 1 FROM validated_lineage l WHERE l.result_row_id=r.result_row_id)''').fetchone()[0]
                        if bad or untraced:
                            raise TraceAtlasError('结果与来源记录无法完整对应，未发布结果。', 'INVALID_RESULT_LINEAGE', 'verifying')
        finally:
            connection.close()

    def commit(self, manifest, task_id, attempt_id):
        self._require_write()
        with self._commit_mutex:
            with self._mutex:
                previous = self.connection.execute("SELECT manifest FROM commits WHERE task_id=? AND state='committed'", (task_id,)).fetchone()
            if previous:
                return json.loads(previous['manifest']).get('summary', {})
            task = self.get_task(task_id)
            if task['current_attempt_id'] != attempt_id:
                raise TraceAtlasError('这是过期任务的产物，未提交。', 'STALE_ATTEMPT', 'committing')
            if task['state'] not in {'running', 'committing'}:
                raise TraceAtlasError('任务当前状态不允许提交。', 'INVALID_COMMIT_STATE', 'committing')
            staging = contained_path(self.path / 'staging', attempt_id)
            self._validate_commit(manifest, staging)
            journal = json.loads(_json(manifest))
            journal['_export_journal'] = []
            for i, item in enumerate(journal.get('exports', [])):
                destination = Path(item['destination']).resolve()
                suffix = f'.traceatlas-{attempt_id}-{i}'
                journal['_export_journal'].append({'destination': str(destination), 'sha256': item['sha256'],
                    'temporary': str(destination.with_name(destination.name + suffix + '.tmp')),
                    'backup': str(destination.with_name(destination.name + suffix + '.bak')),
                    'existed': destination.exists(), 'previous_sha256': file_hash(destination) if destination.exists() else None})
            now = utc_now()
            with self._transaction() as connection:
                connection.execute('INSERT INTO commits VALUES (?,?,?,?,?,?)', (attempt_id, task_id, 'prepared', _json(journal), now, now))
                connection.execute("UPDATE tasks SET state='committing',updated_at=? WHERE task_id=?", (now, task_id))
                connection.execute("UPDATE attempts SET state='committing',updated_at=? WHERE attempt_id=?", (now, attempt_id))
            try:
                for item in journal.get('files', []):
                    source, destination = contained_path(staging, item['source']), contained_path(self.path, item['destination'])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._publish_no_replace(source, destination)
                for item, export in zip(journal.get('exports', []), journal['_export_journal']):
                    source, destination = contained_path(staging, item['source']), Path(export['destination'])
                    temporary, backup = Path(export['temporary']), Path(export['backup'])
                    with source.open('rb') as reader, temporary.open('xb') as writer:
                        shutil.copyfileobj(reader, writer, 1024 * 1024)
                        writer.flush()
                        os.fsync(writer.fileno())
                    if item.get('overwrite', False):
                        if destination.exists():
                            if not export['existed'] or file_hash(destination) != export['previous_sha256']:
                                raise TraceAtlasError('目标文件在导出期间被其他程序更改，请重新选择保存位置。', 'EXPORT_CONFLICT')
                            shutil.copy2(destination, backup)
                        os.replace(temporary, destination)
                    else:
                        self._publish_no_replace(temporary, destination)
                with self._transaction() as connection:
                    for obj in journal.get('objects', []):
                        self._insert_object(connection, obj)
                    for item in journal.get('files', []):
                        ids = [obj['id'] for obj in journal.get('objects', [])
                               if item['destination'] in _json(obj.get('data', {}))]
                        connection.execute('INSERT INTO artifacts VALUES (?,?,?,?,?)',
                                           (item['destination'], item['sha256'], item['size'], attempt_id, _json(ids)))
                    self._invalidate_indexes(connection)
                    final = json.loads(connection.execute('SELECT data FROM tasks WHERE task_id=?', (task_id,)).fetchone()[0])
                    final.update(summary=journal.get('summary', {}), result_ids=[obj['id'] for obj in journal.get('objects', [])], finished_at=utc_now())
                    connection.execute("UPDATE tasks SET state='succeeded',data=?,updated_at=? WHERE task_id=?", (_json(final), utc_now(), task_id))
                    connection.execute("UPDATE attempts SET state='succeeded',data=?,updated_at=? WHERE attempt_id=?", (_json(final), utc_now(), attempt_id))
                    connection.execute("UPDATE commits SET state='committed',updated_at=? WHERE attempt_id=?", (utc_now(), attempt_id))
                self._cleanup_export_journal(journal)
                return journal.get('summary', {})
            except Exception as exc:  # noqa: BLE001 - every publication failure must rollback its journal
                with self._mutex:
                    committed = self.connection.execute("SELECT 1 FROM commits WHERE attempt_id=? AND state='committed'", (attempt_id,)).fetchone()
                if committed:
                    self.recovery_report.append('结果已保存，但部分导出临时文件未能清理。')
                    return journal.get('summary', {})
                rollback_errors = self._rollback_journal(journal)
                error = exc if isinstance(exc, TraceAtlasError) else TraceAtlasError(
                    '结果提交失败，请检查磁盘空间、目录权限和文件占用后重试。', 'COMMIT_FAILED', 'committing', True,
                    {'reason': str(exc), 'recovery_errors': rollback_errors})
                with self._transaction() as connection:
                    connection.execute("UPDATE commits SET state=?,updated_at=? WHERE attempt_id=?",
                                       ('recovery_required' if rollback_errors else 'aborted', utc_now(), attempt_id))
                self.update_task(task_id, 'failed', error=error.to_dict())
                raise error

    @staticmethod
    def _publish_no_replace(source, destination):
        if os.name == 'nt':
            os.rename(source, destination)  # Windows fails if destination already exists.
        else:
            os.link(source, destination)
            source.unlink()

    def _rollback_journal(self, journal):
        errors = []
        for item in journal.get('files', []):
            try:
                path = contained_path(self.path, item['destination'])
                if path.is_file() and file_hash(path) == item['sha256']:
                    path.unlink()
            except OSError as exc:
                errors.append(str(exc))
        for export in journal.get('_export_journal', []):
            try:
                destination, backup = Path(export['destination']), Path(export['backup'])
                if backup.exists() and file_hash(backup) != export.get('previous_sha256'):
                    errors.append('导出备份内容已改变，已保留文件供人工检查。')
                    continue
                if destination.exists() and file_hash(destination) == export['sha256']:
                    if backup.exists():
                        os.replace(backup, destination)
                    elif not export['existed']:
                        destination.unlink()
                elif not destination.exists() and backup.exists():
                    os.replace(backup, destination)
                elif backup.exists():
                    errors.append('导出目标已被修改，保留原文件备份供手动恢复。')
                temporary = Path(export['temporary'])
                if temporary.exists():
                    if file_hash(temporary) == export['sha256']:
                        temporary.unlink()
                    else:
                        errors.append('导出临时文件不完整或内容已改变，已保留供人工处理。')
                if backup.exists() and destination.exists() and file_hash(destination) == export.get('previous_sha256'):
                    backup.unlink()
            except OSError as exc:
                errors.append(str(exc))
        return errors

    @staticmethod
    def _cleanup_export_journal(journal):
        for item in journal.get('_export_journal', []):
            for key, expected in [('temporary', item['sha256']), ('backup', item.get('previous_sha256'))]:
                path = Path(item[key])
                if path.exists():
                    if expected is None or file_hash(path) != expected:
                        raise OSError('恢复日志中的导出临时文件或备份内容已改变，已保留供人工检查。')
                    path.unlink()

    def _recover(self):
        rows = self.connection.execute("SELECT * FROM commits WHERE state IN ('prepared','recovery_required')").fetchall()
        for row in rows:
            errors = self._rollback_journal(json.loads(row['manifest']))
            with self._transaction() as connection:
                connection.execute('UPDATE commits SET state=?,updated_at=? WHERE attempt_id=?',
                                   ('recovery_required' if errors else 'aborted', utc_now(), row['attempt_id']))
            self.recovery_report.append('已识别一次未完成的提交。' + ('仍有文件占用，需要检查诊断。' if errors else '未登记产物已回收。'))
        for row in self.connection.execute("SELECT manifest FROM commits WHERE state='committed'").fetchall():
            try:
                self._cleanup_export_journal(json.loads(row['manifest']))
            except OSError:
                self.recovery_report.append('已提交导出的临时备份仍被占用，正式结果已保存。')
        for task in self.list_tasks():
            if task['state'] in ACTIVE_STATES:
                if self._previous_worker_is_alive(task):
                    self.recovery_report.append('上次任务的工作进程仍在退出或尚无法确认已停止。该任务及临时目录保持保护，请稍后重新打开项目。')
                    continue
                self.update_task(task['task_id'], 'interrupted', error={
                    'code': 'APPLICATION_INTERRUPTED', 'stage': task['state'], 'retryable': True,
                    'message': '上次程序退出时任务尚未完成。已保存的旧结果保留，可重试本任务。'})
                self.recovery_report.append('已将上次未完成任务标记为中断。')
        self._check_artifacts(update=True)
        leftovers = [p for p in (self.path / 'staging').iterdir() if p.is_dir()] if (self.path / 'staging').exists() else []
        if leftovers:
            self.recovery_report.append(f'发现 {len(leftovers)} 个任务临时目录，可在清理操作中回收；它们不会显示为正式结果。')

    @staticmethod
    def _previous_worker_is_alive(task):
        pid = task.get('worker_pid')
        if not pid:
            return False
        import psutil
        try:
            process = psutil.Process(pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return False
            created_at = task.get('worker_created_at')
            # An older task without a creation timestamp cannot prove this PID has
            # been reused; conservatively protect its staging instead of deleting it.
            return created_at is None or abs(process.create_time() - float(created_at)) < 0.01
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, ValueError, TypeError):
            return True

    def _check_artifacts(self, update):
        damaged = set()
        for row in self.connection.execute('SELECT * FROM artifacts').fetchall():
            try:
                path = contained_path(self.path, row['path'])
                valid = path.is_file() and path.stat().st_size == row['size'] and file_hash(path) == row['sha256']
            except OSError:
                valid = False
            if not valid:
                damaged.update(json.loads(row['object_ids']))
                self.recovery_report.append(f'已保存产物缺失或损坏：{row["path"]}。请从完整备份恢复。')
        # A missing original is separate from damaged normalized facts. Saved standard
        # rows and historical results remain browsable; opening the source explains repair.
        changed = True
        objects = self.objects(include_deleted=True)
        source_missing = {obj['id'] for obj in objects if obj['kind'] == 'source' and obj['id'] in damaged}
        damaged -= source_missing
        while changed:
            changed = False
            for obj in objects:
                data = obj['data']
                refs = data.get('dataset_versions', []) + data.get('parents', []) + data.get('source_ids', [])
                refs += [data.get('source_id'), data.get('batch_id'), data.get('object_id')]
                if obj['id'] not in damaged and damaged.intersection(refs):
                    damaged.add(obj['id'])
                    changed = True
        if update and (damaged or source_missing):
            with self._transaction() as connection:
                for object_id in damaged:
                    connection.execute("UPDATE objects SET state='damaged',updated_at=? WHERE id=? AND state<>'deleted'", (utc_now(), object_id))
                for object_id in source_missing:
                    connection.execute("UPDATE objects SET state='source_missing',updated_at=? WHERE id=?", (utc_now(), object_id))
        self._readonly_damaged = damaged | source_missing

    def repair_source(self, source_id, path):
        """Restore the exact original, never silently replace it with a different file."""
        self._require_write()
        with self._mutex:
            source = self.get_object(source_id)
            if source['kind'] != 'source':
                raise TraceAtlasError('所选对象不是原始来源。', 'SOURCE_INVALID')
            replacement = Path(path).resolve()
            if not replacement.is_file() or file_hash(replacement) != source['data']['content_hash']:
                raise TraceAtlasError('文件内容与原始导入文件不一致，不能替换来源。请选择原文件的完整副本。', 'SOURCE_HASH_MISMATCH')
            target = contained_path(self.path, source['data']['stored_path'])
            if replacement != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + '.' + new_id() + '.tmp')
                try:
                    with replacement.open('rb') as reader, temporary.open('xb') as writer:
                        shutil.copyfileobj(reader, writer, 1024 * 1024)
                        writer.flush()
                        os.fsync(writer.fileno())
                    if file_hash(temporary) != source['data']['content_hash']:
                        raise TraceAtlasError('复制期间来源文件发生改变，请重新选择。', 'SOURCE_HASH_MISMATCH')
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            with self._transaction() as connection:
                connection.execute("UPDATE objects SET state='ready',updated_at=? WHERE id=?", (utc_now(), source_id))
            self._readonly_damaged.discard(source_id)
            return self.get_object(source_id)

    def cleanup_staging(self):
        self._require_write()
        removed, errors, total = [], [], 0
        with self._mutex:
            active = {t['current_attempt_id'] for t in self.list_tasks() if t['state'] in ACTIVE_STATES}
            recovery = {row[0] for row in self.connection.execute("SELECT attempt_id FROM commits WHERE state IN ('prepared','recovery_required')")}
            for entry in (self.path / 'staging').iterdir():
                if entry.name in active | recovery or not entry.is_dir() or entry.is_symlink():
                    continue
                checked = contained_path(self.path / 'staging', entry.name)
                try:
                    size = sum(p.stat().st_size for p in checked.rglob('*') if p.is_file())
                    shutil.rmtree(checked)
                    total += size
                    removed.append(entry.name)
                except OSError as exc:
                    errors.append({'attempt_id': entry.name, 'error': str(exc)})
        return {'removed': removed, 'bytes_freed': total, 'errors': errors}

    def backup(self, destination=None):
        """Copy all project facts with an SQLite online backup, including WAL data."""
        destination = Path(destination).resolve() if destination else self.path.parent / '.traceatlas-backups' / (self.path.name + '-' + new_id())
        if destination.is_relative_to(self.path) or destination.exists():
            raise TraceAtlasError('备份位置必须是项目之外的新目录。', 'INVALID_BACKUP_DESTINATION')
        with self._mutex:
            required = sum(p.stat().st_size for p in self.path.rglob('*') if p.is_file())
            destination.parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(destination.parent).free < required + 16 * 1024 * 1024:
                raise TraceAtlasError('磁盘可用空间不足，无法建立完整备份。', 'BACKUP_SPACE')
            try:
                shutil.copytree(self.path, destination, ignore=shutil.ignore_patterns('.write.lock', 'metadata.sqlite*', '.migration.json'))
                target = sqlite3.connect(destination / self.manifest.get('metadata_path', 'metadata.sqlite'))
                try:
                    self.connection.backup(target)
                finally:
                    target.close()
                _write_json(destination / 'backup.json', {'project_id': self.id, 'created_at': utc_now(), 'schema_version': self.manifest['schema_version']})
                return destination
            except Exception as exc:
                raise TraceAtlasError('备份未完成，原项目保持不变。', 'BACKUP_FAILED', details={'reason': str(exc)}) from exc

    @classmethod
    def restore_backup(cls, backup, destination):
        backup, destination = Path(backup).resolve(), Path(destination).resolve()
        if destination.exists() or destination.is_relative_to(backup):
            raise TraceAtlasError('恢复位置必须是尚不存在的新目录。', 'INVALID_RESTORE_DESTINATION')
        cls._read_manifest(backup)
        try:
            shutil.copytree(backup, destination, ignore=shutil.ignore_patterns('.write.lock', '.migration.json'))
            project = cls.open(destination, migrate=True)
            if project._readonly_damaged:
                project.close()
                raise TraceAtlasError('备份中有缺失或损坏的产物，已保留恢复副本供检查。', 'BACKUP_CORRUPT')
            return project
        except TraceAtlasError:
            raise
        except Exception as exc:
            raise TraceAtlasError('无法恢复备份，请检查目录和磁盘空间。', 'RESTORE_FAILED', details={'reason': str(exc)}) from exc

    @staticmethod
    def _migrate(path, manifest):
        if manifest['schema_version'] != 0:
            raise TraceAtlasError('没有可用的项目格式升级路径。', 'MIGRATION_UNSUPPORTED')
        database = contained_path(path, manifest.get('metadata_path', 'metadata.sqlite'))
        source = sqlite3.connect(database)
        try:
            if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise TraceAtlasError('旧项目数据库已损坏，不能开始升级。', 'DATABASE_CORRUPT')
            if source.execute('PRAGMA user_version').fetchone()[0] != 0:
                raise TraceAtlasError('旧项目版本信息不一致。', 'VERSION_MISMATCH')
            source.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            source.close()
        backups = path.parent / '.traceatlas-backups'
        backups.mkdir(exist_ok=True)
        required = sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
        if shutil.disk_usage(backups).free < required + database.stat().st_size * 2 + 16 * 1024 * 1024:
            raise TraceAtlasError('磁盘空间不足，未开始升级。', 'MIGRATION_SPACE')
        backup = backups / (path.name + '-v0-' + new_id())
        shutil.copytree(path, backup, ignore=shutil.ignore_patterns('.write.lock'))
        candidate = path / ('.migration-' + new_id() + '.sqlite')
        shutil.copy2(database, candidate)
        connection = sqlite3.connect(candidate)
        try:
            before = connection.execute('SELECT id,kind,name,data FROM objects ORDER BY id').fetchall()
            columns = {row[1] for row in connection.execute('PRAGMA table_info(objects)')}
            for column, declaration in [('state', "TEXT NOT NULL DEFAULT 'ready'"), ('created_at', "TEXT NOT NULL DEFAULT ''"), ('updated_at', "TEXT NOT NULL DEFAULT ''")]:
                if column not in columns:
                    connection.execute(f'ALTER TABLE objects ADD COLUMN {column} {declaration}')
            # Explicit column order is used by new registrations, preserving old object payloads exactly.
            connection.execute('ALTER TABLE objects RENAME TO legacy_objects')
            connection.executescript(SCHEMA)
            connection.execute('INSERT INTO objects SELECT id,kind,name,state,data,created_at,updated_at FROM legacy_objects')
            connection.execute('DROP TABLE legacy_objects')
            connection.execute('CREATE INDEX IF NOT EXISTS objects_kind_state ON objects(kind,state)')
            connection.execute(f'PRAGMA user_version={PROJECT_SCHEMA_VERSION}')
            connection.commit()
            after = connection.execute('SELECT id,kind,name,data FROM objects ORDER BY id').fetchall()
            if before != after or connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise TraceAtlasError('升级验证失败，原项目和备份保持不变。', 'MIGRATION_VALIDATION_FAILED')
            # Legacy files become explicitly registered integrity-checked artifacts.
            for object_id, kind, name, encoded in before:
                data = json.loads(encoded)
                for key in ('stored_path', 'data_path', 'lineage_path', 'errors_path', 'quality_path'):
                    if data.get(key):
                        artifact = contained_path(path, data[key])
                        if not artifact.is_file():
                            raise TraceAtlasError('旧项目引用的文件缺失，未切换项目格式。', 'MIGRATION_SOURCE_MISSING')
                        relative = data[key]
                        digest = file_hash(artifact)
                        if kind == 'source' and key == 'stored_path' and data.get('content_hash'):
                            # A migration must not bless altered bytes with a new trusted hash.
                            # Check the recovery copy too, including changes during backup.
                            saved_source = contained_path(backup, relative)
                            if (digest != data['content_hash'] or not saved_source.is_file()
                                    or file_hash(saved_source) != data['content_hash']):
                                raise TraceAtlasError('旧项目原文件或升级备份与保存的内容摘要不一致，未切换项目格式。'
                                                      '请先找回原文件或恢复完整备份。',
                                                      'MIGRATION_SOURCE_HASH_MISMATCH')
                        connection.execute('INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)', (relative, digest, artifact.stat().st_size, 'migration-v0', _json([object_id])))
            connection.commit()
        finally:
            connection.close()
        journal = {'backup': str(backup), 'candidate': candidate.name, 'database': manifest.get('metadata_path', 'metadata.sqlite')}
        _write_json(path / '.migration.json', journal)
        try:
            os.replace(candidate, database)
            upgraded = dict(manifest, schema_version=PROJECT_SCHEMA_VERSION, migrated_at=utc_now())
            _write_json(path / 'project.json', upgraded)
            (path / '.migration.json').unlink()
        except Exception:
            Project._recover_migration(path)
            raise

    @staticmethod
    def _recover_migration(path):
        journal_path = path / '.migration.json'
        if not journal_path.exists():
            return
        journal = json.loads(journal_path.read_text(encoding='utf-8'))
        backup = Path(journal['backup']).resolve()
        backup_manifest = Project._read_manifest(backup)
        current = Project._read_manifest(path)
        if backup_manifest['project_id'] != current['project_id']:
            raise TraceAtlasError('升级备份与项目标识不一致，请人工检查。', 'MIGRATION_BACKUP_MISMATCH')
        database = contained_path(path, journal['database'])
        shutil.copy2(contained_path(backup, journal['database']), database)
        shutil.copy2(backup / 'project.json', path / 'project.json')
        contained_path(path, journal['candidate']).unlink(missing_ok=True)
        for suffix in ('-wal', '-shm'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        journal_path.unlink()
