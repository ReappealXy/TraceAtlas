import hashlib
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from traceatlas.contracts import (
    Entity,
    RecordProvenance,
    Relation,
    SourceLocator,
    TaskSpec,
    TraceAtlasError,
    Transform,
    new_id,
)
from traceatlas.project import Project, file_hash
from traceatlas.registry import CORE_MANIFEST, ModuleRegistry


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / '审计项目 01', '审计项目 01')
    yield project
    project.close()


def staged(project, content=b'facts', kind='result'):
    task = project.create_task({'feature_id': 'test', 'parameters': {}})
    project.update_task(task['task_id'], 'running')
    root = project.path / 'staging' / task['attempt_id']
    root.mkdir()
    (root / 'fact.txt').write_bytes(content)
    object_id = new_id()
    destination = f'results/{object_id}/fact.txt'
    manifest = {'files': [{'source': 'fact.txt', 'destination': destination,
                            'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}],
                'objects': [{'id': object_id, 'kind': kind, 'name': '真实产物', 'data': {'data_path': destination}}],
                'summary': {'count': 1}}
    return task, manifest, object_id


def test_create_reopen_move_and_session(project, tmp_path):
    project.save_session({'selection': '保存的筛选条件', 'filters': [{'column': 'account', 'value': '00123'}]})
    project_id = project.id
    original = project.path
    project.close()
    moved = tmp_path / '移动 后的项目'
    original.rename(moved)
    with Project.open(moved) as opened:
        assert opened.id == project_id
        assert opened.load_session()['filters'][0]['value'] == '00123'


def test_create_never_overwrites_existing(project):
    with pytest.raises(TraceAtlasError, match='已存在'):
        Project.create(project.path, 'same')


def test_single_writer_with_concurrent_reader(project):
    with pytest.raises(TraceAtlasError) as error:
        Project.open(project.path)
    assert error.value.code == 'PROJECT_LOCKED'
    with Project.open(project.path, readonly=True) as reader:
        assert reader.id == project.id
        project.save_session({'changed': True})
        assert reader.load_session()['changed']
        with pytest.raises(TraceAtlasError) as denied:
            reader.save_session({})
        assert denied.value.code == 'READ_ONLY'


def test_single_writer_across_processes(project):
    script = ('from traceatlas.project import Project; from traceatlas.contracts import TraceAtlasError; '
              'import sys\ntry: Project.open(sys.argv[1])\nexcept TraceAtlasError as e: print(e.code)')
    result = subprocess.run([sys.executable, '-c', script, str(project.path)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'PROJECT_LOCKED'


def test_future_project_is_rejected_without_writing(project):
    path = project.path
    project.close()
    manifest = json.loads((path / 'project.json').read_text(encoding='utf-8'))
    manifest['schema_version'] = 99
    (path / 'project.json').write_text(json.dumps(manifest), encoding='utf-8')
    before = file_hash(path / 'metadata.sqlite')
    with pytest.raises(TraceAtlasError) as failure:
        Project.open(path)
    assert failure.value.code == 'PROJECT_TOO_NEW'
    assert file_hash(path / 'metadata.sqlite') == before


def test_commit_is_durable_and_idempotent(project):
    task, manifest, object_id = staged(project)
    expected = project.commit(manifest, task['task_id'], task['attempt_id'])
    assert project.get_task(task['task_id'])['state'] == 'succeeded'
    assert project.commit(manifest, task['task_id'], task['attempt_id']) == expected
    assert len(project.objects()) == 1
    path = project.path
    project.close()
    with Project.open(path) as reopened:
        assert reopened.get_object(object_id)['state'] == 'ready'
        assert reopened.get_task(task['task_id'])['state'] == 'succeeded'


def test_hash_mismatch_and_path_traversal_never_publish(project):
    task, manifest, _ = staged(project)
    manifest['files'][0]['sha256'] = '0' * 64
    with pytest.raises(TraceAtlasError) as mismatch:
        project.commit(manifest, task['task_id'], task['attempt_id'])
    assert mismatch.value.code == 'ARTIFACT_MISMATCH'
    manifest['files'][0]['source'] = '../outside'
    with pytest.raises(TraceAtlasError) as escape:
        project.commit(manifest, task['task_id'], task['attempt_id'])
    assert escape.value.code == 'INVALID_PATH'
    assert project.objects() == []


def test_disk_full_rolls_back_and_retry_uses_new_attempt(project, monkeypatch):
    task, manifest, _ = staged(project)
    def disk_full(*args):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(project, '_publish_no_replace', disk_full)
    with pytest.raises(TraceAtlasError) as failure:
        project.commit(manifest, task['task_id'], task['attempt_id'])
    assert failure.value.code == 'COMMIT_FAILED'
    assert project.get_task(task['task_id'])['state'] == 'failed'
    attempt = project.create_attempt(task['task_id'])
    assert attempt['attempt_id'] != task['attempt_id']
    assert len(project.task_attempts(task['task_id'])) == 2
    assert project.objects() == []


def test_interruption_after_file_move_recovers_old_facts(project, monkeypatch):
    task, manifest, _object_id = staged(project)
    original_publish = project._publish_no_replace
    def crash_after_move(source, destination):
        original_publish(source, destination)
        raise KeyboardInterrupt('simulate process termination after move')
    monkeypatch.setattr(project, '_publish_no_replace', crash_after_move)
    with pytest.raises(KeyboardInterrupt):
        project.commit(manifest, task['task_id'], task['attempt_id'])
    assert (project.path / manifest['files'][0]['destination']).exists()
    path = project.path
    project.close()
    with Project.open(path) as recovered:
        assert recovered.get_task(task['task_id'])['state'] == 'interrupted'
        assert recovered.objects() == []
        assert not (path / manifest['files'][0]['destination']).exists()
        assert recovered.recovery_report
        retried = recovered.create_attempt(task['task_id'])
        assert retried['attempt_id'] != task['attempt_id']


def test_interruption_inside_sqlite_transaction_does_not_publish(project, monkeypatch):
    task, manifest, _ = staged(project)
    original_insert = project._insert_object
    def crash_after_insert(connection, obj):
        original_insert(connection, obj)
        raise KeyboardInterrupt('simulate process termination in transaction')
    monkeypatch.setattr(project, '_insert_object', crash_after_insert)
    with pytest.raises(KeyboardInterrupt):
        project.commit(manifest, task['task_id'], task['attempt_id'])
    path = project.path
    project.close()
    with Project.open(path) as recovered:
        assert recovered.objects() == []
        assert recovered.get_task(task['task_id'])['state'] == 'interrupted'


def test_missing_committed_data_is_damaged(project):
    task, manifest, object_id = staged(project)
    project.commit(manifest, task['task_id'], task['attempt_id'])
    (project.path / manifest['files'][0]['destination']).unlink()
    with Project.open(project.path, readonly=True) as reader:
        assert reader.get_object(object_id)['state'] == 'damaged'
    path = project.path
    project.close()
    with Project.open(path) as recovered:
        assert recovered.get_object(object_id)['state'] == 'damaged'
        assert recovered.get_task(task['task_id'])['state'] == 'succeeded'
        assert recovered.recovery_report


def test_source_repair_requires_identical_content(project, tmp_path):
    task, manifest, object_id = staged(project, content=b'original source', kind='source')
    file = manifest['files'][0]
    manifest['objects'][0]['data'] = {'stored_path': file['destination'], 'content_hash': file['sha256']}
    project.commit(manifest, task['task_id'], task['attempt_id'])
    (project.path / file['destination']).unlink()
    original = tmp_path / '原文件.xlsx'
    original.write_bytes(b'original source')
    wrong = tmp_path / '其他文件.xlsx'
    wrong.write_bytes(b'different')
    path = project.path
    project.close()
    with Project.open(path) as recovered:
        assert recovered.get_object(object_id)['state'] == 'source_missing'
        with pytest.raises(TraceAtlasError) as failure:
            recovered.repair_source(object_id, wrong)
        assert failure.value.code == 'SOURCE_HASH_MISMATCH'
        repaired = recovered.repair_source(object_id, original)
        assert repaired['state'] == 'ready'
        assert repaired['id'] == object_id
        assert file_hash(path / file['destination']) == file['sha256']


def test_export_failure_restores_previous_output(project, tmp_path, monkeypatch):
    task, _, _ = staged(project, content=b'new output')
    staging = project.path / 'staging' / task['attempt_id']
    (staging / 'meta.json').write_bytes(b'new metadata')
    output, meta = tmp_path / 'output.csv', tmp_path / 'output.metadata.json'
    output.write_bytes(b'old output')
    meta.write_bytes(b'old metadata')
    manifest = {'files': [], 'objects': [], 'exports': []}
    for source, destination in [('fact.txt', output), ('meta.json', meta)]:
        file = staging / source
        manifest['exports'].append({'source': source, 'destination': str(destination), 'size': file.stat().st_size,
                                    'sha256': file_hash(file), 'overwrite': True})
    original_copy = __import__('shutil').copy2
    def fail_second_backup(source, target, *args, **kwargs):
        if Path(source) == meta:
            raise OSError('file locked')
        return original_copy(source, target, *args, **kwargs)
    monkeypatch.setattr('traceatlas.project.shutil.copy2', fail_second_backup)
    with pytest.raises(TraceAtlasError):
        project.commit(manifest, task['task_id'], task['attempt_id'])
    assert output.read_bytes() == b'old output'
    assert meta.read_bytes() == b'old metadata'
    assert project.get_task(task['task_id'])['state'] == 'failed'


def test_export_conflict_requires_explicit_overwrite(project, tmp_path):
    task, _, _ = staged(project)
    destination = tmp_path / 'existing.csv'
    destination.write_bytes(b'keep')
    manifest = {'exports': [{'source': 'fact.txt', 'destination': str(destination),
                            'sha256': hashlib.sha256(b'facts').hexdigest(), 'size': 5}]}
    with pytest.raises(TraceAtlasError) as failure:
        project.commit(manifest, task['task_id'], task['attempt_id'])
    assert failure.value.code == 'FILE_EXISTS'
    assert destination.read_bytes() == b'keep'


def test_export_cannot_overwrite_project_database_even_with_permission(project):
    task, _, _ = staged(project)
    manifest = {'exports': [{'source': 'fact.txt', 'destination': str(project.path / 'metadata.sqlite'),
                            'sha256': hashlib.sha256(b'facts').hexdigest(), 'size': 5, 'overwrite': True}]}
    with pytest.raises(TraceAtlasError) as failure:
        project.commit(manifest, task['task_id'], task['attempt_id'])
    assert failure.value.code == 'INVALID_EXPORT_DESTINATION'
    assert project.connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_same_size_file_tampering_detected_on_reopen(project):
    task, manifest, object_id = staged(project)
    project.commit(manifest, task['task_id'], task['attempt_id'])
    (project.path / manifest['files'][0]['destination']).write_bytes(b'FAKES')
    path = project.path
    project.close()
    with Project.open(path) as recovered:
        assert recovered.get_object(object_id)['state'] == 'damaged'


def test_export_cleanup_preserves_changed_or_unregistered_temporary_files(project, tmp_path):
    temporary, backup, unrelated = tmp_path / 'export.tmp', tmp_path / 'export.bak', tmp_path / 'unregistered.tmp'
    temporary.write_bytes(b'changed')
    backup.write_bytes(b'old data')
    unrelated.write_bytes(b'personal data')
    journal = {'_export_journal': [{'temporary': str(temporary), 'backup': str(backup),
                                  'sha256': hashlib.sha256(b'expected').hexdigest(), 'previous_sha256': file_hash(backup)}]}
    with pytest.raises(OSError):
        project._cleanup_export_journal(journal)
    assert temporary.read_bytes() == b'changed'
    assert backup.read_bytes() == b'old data'
    assert unrelated.read_bytes() == b'personal data'


def legacy_project(path):
    path.mkdir()
    (path / 'staging').mkdir()
    (path / 'sources').mkdir()
    (path / 'sources' / 'legacy.bin').write_bytes(b'00123,12345678901234567890.123456789012,source-row-3')
    manifest = {'application': 'TraceAtlas', 'project_id': new_id(), 'name': '真实旧格式项目',
                'schema_version': 0, 'metadata_path': 'metadata.sqlite', 'created_at': '2026-01-01T00:00:00Z'}
    (path / 'project.json').write_text(json.dumps(manifest), encoding='utf-8')
    connection = sqlite3.connect(path / 'metadata.sqlite')
    connection.execute('CREATE TABLE objects(id TEXT PRIMARY KEY,kind TEXT,name TEXT,data TEXT)')
    payload = {'stored_path': 'sources/legacy.bin', 'amount': '12345678901234567890.123456789012',
               'source_row': 3, 'owner_account': '00123', 'content_hash': file_hash(path / 'sources/legacy.bin')}
    connection.execute('INSERT INTO objects VALUES (?,?,?,?)', ('source-legacy', 'source', '旧来源', json.dumps(payload)))
    connection.commit()
    connection.close()
    return payload


def test_migration_retains_data_and_has_restorable_backup(tmp_path):
    path = tmp_path / '旧项目'
    payload = legacy_project(path)
    with pytest.raises(TraceAtlasError) as failure:
        Project.open(path)
    assert failure.value.code == 'MIGRATION_REQUIRED'
    with Project.open(path, migrate=True) as migrated:
        assert migrated.manifest['schema_version'] == 1
        assert migrated.get_object('source-legacy')['data'] == payload
        migrated.put_object({'id': new_id(), 'kind': 'note', 'name': '迁移后可写', 'data': {'text': 'new'}})
    backups = list((tmp_path / '.traceatlas-backups').iterdir())
    assert len(backups) == 1
    assert json.loads((backups[0] / 'project.json').read_text(encoding='utf-8'))['schema_version'] == 0
    restored = Project.restore_backup(backups[0], tmp_path / '恢复后的项目')
    try:
        assert restored.get_object('source-legacy')['data'] == payload
    finally:
        restored.close()


@pytest.mark.parametrize('change_after_backup', [False, True])
def test_migration_rejects_same_size_source_tampering(tmp_path, monkeypatch, change_after_backup):
    path = tmp_path / '旧项目'
    expected = legacy_project(path)
    source = path / expected['stored_path']
    original = source.read_bytes()
    changed = original.replace(b'00123', b'00999', 1)
    assert len(changed) == len(original)
    database_before = file_hash(path / 'metadata.sqlite')
    manifest_before = (path / 'project.json').read_bytes()
    if change_after_backup:
        original_copytree = __import__('shutil').copytree
        def change_source_after_copy(*args, **kwargs):
            copied = original_copytree(*args, **kwargs)
            source.write_bytes(changed)
            return copied
        monkeypatch.setattr('traceatlas.project.shutil.copytree', change_source_after_copy)
    else:
        source.write_bytes(changed)
    with pytest.raises(TraceAtlasError) as failure:
        Project.open(path, migrate=True)
    assert failure.value.code == 'MIGRATION_SOURCE_HASH_MISMATCH'
    assert file_hash(path / 'metadata.sqlite') == database_before
    assert (path / 'project.json').read_bytes() == manifest_before
    assert source.read_bytes() == changed
    assert not (path / '.migration.json').exists()
    backups = list((tmp_path / '.traceatlas-backups').iterdir())
    assert len(backups) == 1
    assert json.loads((backups[0] / 'project.json').read_text(encoding='utf-8'))['schema_version'] == 0
    with sqlite3.connect(path / 'metadata.sqlite') as connection:
        assert json.loads(connection.execute('SELECT data FROM objects').fetchone()[0]) == expected


def test_migration_interruption_recovers_original_before_retry(tmp_path, monkeypatch):
    path = tmp_path / '旧项目'
    expected = legacy_project(path)
    original_replace = __import__('os').replace
    def crash_after_database_switch(source, destination):
        original_replace(source, destination)
        if Path(destination).name == 'metadata.sqlite':
            raise KeyboardInterrupt('power failure during format switch')
    monkeypatch.setattr('traceatlas.project.os.replace', crash_after_database_switch)
    with pytest.raises(KeyboardInterrupt):
        Project.open(path, migrate=True)
    # The interrupted object was never returned: release its lock on process exit is
    # modelled by explicitly closing it via the exception-safe open implementation.
    monkeypatch.setattr('traceatlas.project.os.replace', original_replace)
    with Project.open(path, migrate=True) as recovered:
        assert recovered.get_object('source-legacy')['data'] == expected
        assert not (path / '.migration.json').exists()


def test_backup_includes_committed_data_and_recovers_elsewhere(project, tmp_path):
    task, manifest, object_id = staged(project)
    project.commit(manifest, task['task_id'], task['attempt_id'])
    backup = project.backup()
    with Project.restore_backup(backup, tmp_path / '异地恢复') as restored:
        assert restored.get_object(object_id)['data'] == project.get_object(object_id)['data']
        assert restored.get_task(task['task_id'])['state'] == 'succeeded'
        assert (restored.path / manifest['files'][0]['destination']).read_bytes() == b'facts'


def test_deleting_result_preserves_sources_and_blocks_dataset_references(project):
    project.put_object({'id': 'dataset', 'kind': 'dataset', 'name': '输入', 'data': {}})
    project.put_object({'id': 'source', 'kind': 'source', 'name': '原文件', 'data': {}})
    project.put_object({'id': 'result', 'kind': 'result', 'name': '结果', 'data': {'dataset_versions': ['dataset']}})
    with pytest.raises(TraceAtlasError) as failure:
        project.delete_object('dataset')
    assert failure.value.code == 'OBJECT_REFERENCED'
    project.delete_object('result')
    project.delete_object('dataset')
    assert project.get_object('source')['state'] == 'ready'
    assert [obj['id'] for obj in project.objects()] == ['source']


def test_cleanup_never_removes_active_attempt(project):
    task, _, _ = staged(project)
    inactive = project.path / 'staging' / 'inactive-attempt'
    inactive.mkdir()
    (inactive / 'partial.bin').write_bytes(b'partial')
    report = project.cleanup_staging()
    assert report['removed'] == ['inactive-attempt']
    assert report['bytes_freed'] == 7
    assert (project.path / 'staging' / task['attempt_id']).exists()


def test_registry_atomic_duplicate_rejection_and_optional_failure_isolation():
    registry = ModuleRegistry()
    duplicate = deepcopy(CORE_MANIFEST)
    duplicate['module_id'] = 'other'
    with pytest.raises(TraceAtlasError) as failure:
        registry.register(duplicate)
    assert failure.value.code == 'DUPLICATE_FEATURE'
    assert len(registry.list_modules()) == 1
    assert registry.load_optional(lambda: (_ for _ in ()).throw(ImportError('optional dependency missing'))) is None
    assert len(registry.failures) == 1
    assert registry.get_feature('xlsx.import')['module_version'] == '1.0.0'
    assert not next(f for f in registry.available_features([]) if f['feature_id'] == 'index.rebuild')['available']
    assert next(f for f in registry.available_features(['dataset']) if f['feature_id'] == 'index.rebuild')['available']


def test_registry_rejects_wrong_parameter_type():
    registry = ModuleRegistry()
    with pytest.raises(TraceAtlasError) as failure:
        registry.validate_parameters('xlsx.import', {'path': 'x.xlsx', 'sheet': 'Sheet1', 'mapping': {}, 'header_row': True})
    assert failure.value.code == 'PARAMETER_TYPE'


def test_many_to_one_provenance_preserves_every_source():
    refs = tuple(SourceLocator('source', sheet='明细', row=row) for row in (2, 3, 4))
    merged = RecordProvenance('stable-id', 'version-1', refs).to_dict()
    assert [source['row'] for source in merged['source_refs']] == [2, 3, 4]
    with pytest.raises(TraceAtlasError):
        RecordProvenance('bad', 'version-1', ()).to_dict()


def test_entity_relation_transform_and_task_contracts_freeze_versions():
    account = Entity('entity-1', 'account', 'bank:account', '00123').to_dict()
    assert account['business_key'] == '00123'
    provenance = RecordProvenance('record-1', 'version-1', (SourceLocator('source-1', sheet='流水', row=2),))
    relation = Relation('relation-1', 'entity-1', 'entity-2', 'transfer', (provenance,)).to_dict()
    assert relation['record_refs'][0]['source_refs'][0]['row'] == 2
    transform = Transform('transform-1', 'rule-1', '1.0.0', ('version-1',), ('version-2',), {}).to_dict()
    assert transform['rule_version'] == '1.0.0'
    spec = TaskSpec('task', 'project', 'attempt', 'feature', '1.0.0', ('version-1',), {}).to_dict()
    assert spec['parameter_schema_version'] == 1
    with pytest.raises(TraceAtlasError):
        TaskSpec('task', 'project', 'attempt', 'feature', '1.0.0', (), {}, parameter_schema_version=2).to_dict()
