"""Explicit module manifests; optional failures never disable the core project."""
from __future__ import annotations

from copy import deepcopy

from .contracts import PROTOCOL_VERSION, TraceAtlasError

CORE_MANIFEST = {
    'module_id': 'traceatlas.core', 'name': 'A · 基础平台与数据契约', 'version': '1.0.0',
    'protocol_version': 1, 'schemas': {'transactions': [1]}, 'dependencies': [],
    'features': [
        {'feature_id': 'xlsx.import', 'name': '导入 Excel', 'required_input_kind': None,
         'return_type': 'dataset', 'resource_requirements': {'worker_memory_bytes': 4 * 1024**3},
         'parameter_schema': {'required': ['path', 'sheet', 'mapping'], 'properties': {
             'path': {'type': 'string'}, 'sheet': {'type': 'string'}, 'mapping': {'type': 'object'},
             'header_row': {'type': 'integer', 'minimum': 1}, 'start_row': {'type': 'integer', 'minimum': 1},
             'end_row': {'type': ['integer', 'null'], 'minimum': 1},
             'platform': {'type': 'string'}, 'account_namespace': {'type': 'string'},
             'currency': {'type': 'string'}, 'timezone': {'type': 'string'}, 'allow_partial': {'type': 'boolean'},
             'expected_source_hash': {'type': 'string'}}}},
        {'feature_id': 'validation.account_count', 'name': '按账户计数（验证任务）', 'required_input_kind': 'dataset',
         'return_type': 'result', 'parameter_schema': {'required': [], 'properties': {}}},
        {'feature_id': 'result.export', 'name': '导出数据或结果', 'required_input_kind': ['dataset', 'result'],
         'return_type': 'export', 'parameter_schema': {'required': ['object_id', 'destination', 'format', 'scope'],
          'properties': {'object_id': {'type': 'string'}, 'destination': {'type': 'string'},
             'format': {'enum': ['xlsx', 'csv']}, 'scope': {'enum': ['all', 'filtered']},
             'filters': {'type': ['array', 'null']}, 'sort': {'type': ['string', 'null']},
             'descending': {'type': 'boolean'}, 'overwrite': {'type': 'boolean'}}}},
        {'feature_id': 'index.rebuild', 'name': '重建项目账户索引', 'required_input_kind': 'dataset',
         'return_type': 'index', 'parameter_schema': {'required': [], 'properties': {}}},
    ],
}


class ModuleRegistry:
    def __init__(self, builtins=True):
        self._modules = {}
        self._features = {}
        self.failures = []
        if builtins:
            self.register(CORE_MANIFEST)

    def register(self, manifest):
        manifest = deepcopy(manifest)
        required = {'module_id', 'version', 'protocol_version', 'schemas', 'features', 'dependencies'}
        if not isinstance(manifest, dict) or not required.issubset(manifest):
            raise TraceAtlasError('模块清单缺少必要字段。', 'INVALID_MODULE_MANIFEST')
        module_id = manifest['module_id']
        if not module_id or module_id in self._modules:
            raise TraceAtlasError('模块标识重复或为空。', 'DUPLICATE_MODULE')
        if manifest['protocol_version'] != PROTOCOL_VERSION:
            raise TraceAtlasError('模块通信协议与当前程序不兼容。', 'MODULE_PROTOCOL_MISMATCH')
        missing = [dependency for dependency in manifest['dependencies'] if dependency not in self._modules]
        if missing:
            raise TraceAtlasError('模块依赖尚未安装或不可用。', 'MODULE_DEPENDENCY_MISSING', details={'missing': missing})
        seen = set()
        for feature in manifest['features']:
            feature_id = feature.get('feature_id')
            if not feature_id or feature_id in seen or feature_id in self._features:
                raise TraceAtlasError('功能标识重复或为空，已拒绝注册整个模块。', 'DUPLICATE_FEATURE')
            seen.add(feature_id)
            if not isinstance(feature.get('parameter_schema'), dict) or not feature.get('return_type'):
                raise TraceAtlasError('功能缺少参数或返回类型契约。', 'INVALID_FEATURE_MANIFEST')
            feature.update(module_id=module_id, module_version=manifest['version'])
            feature.setdefault('parameter_schema_version', 1)
        # Registration is all-or-nothing, including a duplicate in the last feature.
        self._modules[module_id] = manifest
        for feature in manifest['features']:
            self._features[feature['feature_id']] = feature
        return deepcopy(manifest)

    def load_optional(self, factory, name='可选模块'):
        try:
            return self.register(factory())
        except Exception as exc:  # noqa: BLE001 - optional plugin failures are isolated from the core application
            error = {'name': name, 'message': str(exc), 'code': getattr(exc, 'code', 'MODULE_LOAD_FAILED')}
            self.failures.append(error)
            return None

    def get_feature(self, feature_id):
        if feature_id not in self._features:
            raise TraceAtlasError('该功能未安装或当前版本不支持。', 'FEATURE_UNAVAILABLE')
        return deepcopy(self._features[feature_id])

    def list_modules(self):
        return deepcopy(list(self._modules.values()))

    def list_features(self):
        return deepcopy(list(self._features.values()))

    def available_features(self, input_kinds=None):
        kinds = set(input_kinds or [])
        features = self.list_features()
        for feature in features:
            required = feature.get('required_input_kind')
            required = set(required if isinstance(required, list) else [required]) if required else set()
            feature['available'] = not required or bool(required & kinds)
            feature['unavailable_reason'] = '' if feature['available'] else '请先选择所需的数据集或结果。'
        return features

    def validate_parameters(self, feature_id, parameters):
        feature = self.get_feature(feature_id)
        if not isinstance(parameters, dict):
            raise TraceAtlasError('任务参数必须是对象。', 'INVALID_PARAMETERS')
        schema = feature['parameter_schema']
        missing = [key for key in schema.get('required', []) if key not in parameters or parameters[key] in ('', None)]
        if missing:
            raise TraceAtlasError('请填写必要参数：' + '、'.join(missing), 'MISSING_PARAMETERS', details={'missing': missing})
        types = {'string': str, 'object': dict, 'array': list, 'integer': int, 'number': (int, float), 'boolean': bool, 'null': type(None)}
        for key, rule in schema.get('properties', {}).items():
            if key not in parameters:
                continue
            value = parameters[key]
            accepted = rule.get('type', [])
            accepted = [accepted] if isinstance(accepted, str) else accepted
            if accepted and not any(isinstance(value, types[t]) and not (t in {'integer', 'number'} and isinstance(value, bool)) for t in accepted):
                raise TraceAtlasError(f'参数 {key} 的类型不正确。', 'PARAMETER_TYPE')
            if 'enum' in rule and value not in rule['enum']:
                raise TraceAtlasError(f'参数 {key} 的选项不受支持。', 'PARAMETER_VALUE')
            if value is not None and 'minimum' in rule and value < rule['minimum']:
                raise TraceAtlasError(f'参数 {key} 超出允许范围。', 'PARAMETER_RANGE')
        return deepcopy(parameters)

    def validate_task(self, spec):
        """Never silently run a persisted task with a different implementation."""
        feature = self.get_feature(spec.get('feature_id'))
        if spec.get('protocol_version') != PROTOCOL_VERSION:
            raise TraceAtlasError('任务通信协议与当前程序不兼容，请使用兼容版本或重新创建任务。',
                                  'TASK_SCHEMA_UNSUPPORTED')
        if spec.get('module_version') != feature['module_version']:
            raise TraceAtlasError('原任务的模块版本与当前程序不同，请保留历史结果并重新创建任务。',
                                  'TASK_VERSION_UNSUPPORTED')
        if spec.get('parameter_schema_version') != feature['parameter_schema_version']:
            raise TraceAtlasError('原任务的参数格式与当前程序不兼容，请重新确认参数并创建任务。',
                                  'TASK_SCHEMA_UNSUPPORTED')
        return self.validate_parameters(spec['feature_id'], spec.get('parameters'))
