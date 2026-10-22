# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys

# Do not collect incompatible DLLs from unrelated applications on the developer PATH.
# Qt 6 uses Windows' system ICU; a Poppler ICU with suffixed exports cannot replace it.
os.environ['PATH'] = os.pathsep.join([str(Path(sys.executable).parent),
                                    str(Path(os.environ['SystemRoot']) / 'System32'),
                                    os.environ['SystemRoot']])

root = Path(SPECPATH).parent
common = dict(pathex=[str(root / 'src')], binaries=[],
              datas=[(str(root / 'LICENSE'), '.')],
              hiddenimports=['duckdb', 'pyarrow', 'pyarrow.parquet', 'psutil'],
              hookspath=[], hooksconfig={}, runtime_hooks=[],
              excludes=['tkinter', 'unittest', 'pytest', 'PySide6.QtWebEngineCore',
                        'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
                        'PySide6.QtQml', 'PySide6.QtQuick'], noarchive=False)
app_analysis = Analysis([str(root / 'scripts/app_entry.py')], **common)
# The Widgets application does not use PDF image decoding or Qt's virtual
# keyboard. Their auto-collected plugins pull in unused PDF/QML/Quick modules;
# the virtual-keyboard module also has a different open-source license choice.
unused_qt_binaries = {
    'qtvirtualkeyboardplugin.dll', 'qpdf.dll', 'Qt6VirtualKeyboard.dll', 'Qt6Pdf.dll',
    'Qt6Qml.dll', 'Qt6Quick.dll', 'Qt6QmlMeta.dll', 'Qt6QmlModels.dll',
    'Qt6QmlWorkerScript.dll', 'Qt6OpenGL.dll',
}
app_analysis.binaries = [entry for entry in app_analysis.binaries
                         if Path(entry[0]).name not in unused_qt_binaries]
app_pyz = PYZ(app_analysis.pure)
app_exe = EXE(app_pyz, app_analysis.scripts, [], exclude_binaries=True, name='TraceAtlas',
              debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
              console=False, disable_windowed_traceback=False)
worker_common = dict(common)
worker_common['excludes'] = [*common['excludes'], 'PySide6']
worker_analysis = Analysis([str(root / 'scripts/worker_entry.py')], **worker_common)
worker_pyz = PYZ(worker_analysis.pure)
worker_exe = EXE(worker_pyz, worker_analysis.scripts, [], exclude_binaries=True,
                 name='TraceAtlasWorker', debug=False, strip=False, upx=False, console=True)
bundle = COLLECT(app_exe, worker_exe, app_analysis.binaries, app_analysis.datas,
                 worker_analysis.binaries, worker_analysis.datas,
                 strip=False, upx=False, name='TraceAtlas')
