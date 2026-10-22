"""Copy installed runtime distribution notices into the deliverable."""
import hashlib
import json
import shutil
import sqlite3
import sys
from importlib.metadata import distribution
from pathlib import Path

PACKAGES = ['PySide6', 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6', 'duckdb',
            'pyarrow', 'openpyxl', 'et_xmlfile', 'psutil', 'pyinstaller']
QT_PACKAGES = {'PySide6', 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6'}


def copy_qt_licenses(output):
    source = Path(__file__).resolve().parents[1] / 'packaging' / 'licenses' / 'Qt'
    manifest = json.loads((source / 'SOURCES.json').read_text(encoding='utf-8'))
    for item in manifest['files']:
        if hashlib.sha256((source / item['file']).read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError(f"Official Qt license text changed: {item['file']}")
    target = output / 'Qt'
    target.mkdir(exist_ok=True)
    notices = []
    for origin in sorted(source.iterdir()):
        if origin.is_file():
            destination = target / origin.name
            shutil.copyfile(origin, destination)
            notices.append(str(destination.relative_to(output)))
    return notices


def main():
    bundle = Path(sys.argv[1])
    output = bundle / 'third-party-licenses'
    output.mkdir(parents=True, exist_ok=True)
    qt_notices = copy_qt_licenses(output)
    qt_root = bundle / '_internal' / 'PySide6'
    qt_binaries = sorted(str(path.relative_to(bundle)) for path in qt_root.rglob('*')
                         if path.is_file() and path.suffix.lower() in {'.dll', '.pyd'})
    inventory = {'python': sys.version, 'sqlite': sqlite3.sqlite_version,
                 'qt_binaries': qt_binaries, 'packages': []}
    for name in PACKAGES:
        dist = distribution(name)
        target = output / name
        target.mkdir(exist_ok=True)
        files = []
        for item in dist.files or []:
            if any(part.lower().startswith(('license', 'licence', 'copying', 'notice')) for part in item.parts):
                origin = Path(dist.locate_file(item))
                if origin.is_file():
                    destination = target / Path(*item.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(origin, destination)
                    files.append(str(destination.relative_to(output)))
        license_label = dist.metadata.get('License-Expression') or dist.metadata.get('License') or ''
        if name in QT_PACKAGES:
            files.extend(qt_notices)
        inventory['packages'].append({'name': name, 'version': dist.version,
                                       'license': license_label, 'notices': files})
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.exists():
        shutil.copyfile(python_license, output / 'Python-LICENSE.txt')
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding='utf-8')
    (output / 'README.txt').write_text(
        'TraceAtlas uses third-party components under their respective licenses.\n'
        'Qt/PySide6 is dynamically linked. Qt library files in _internal can be replaced with compatible builds.\n'
        'Source and license information: https://www.qt.io/qt-for-python ; https://code.qt.io/\n'
        'Qt/ contains official open-source license texts, exact source URLs and checksums.\n'
        'These upstream alternatives do not change the license of TraceAtlas itself.\n'
        'See the copied notices and inventory.json for the exact distributed versions.\n', encoding='utf-8')


if __name__ == '__main__':
    main()
