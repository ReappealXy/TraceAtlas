"""Desktop entry point; frozen and source launches share the worker protocol."""

from __future__ import annotations

import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog="TraceAtlas")
    parser.add_argument("--worker", metavar="SPEC", help=argparse.SUPPRESS)
    parser.add_argument("--project", metavar="DIRECTORY")
    parser.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.worker:
        from .worker import main as worker_main

        return worker_main(options.worker)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("TraceAtlas")
    app.setOrganizationName("TraceAtlas")
    app.setApplicationVersion("0.1.0")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    if options.project:
        QTimer.singleShot(0, lambda: window.open_project(options.project))
    if options.screenshot:
        QTimer.singleShot(2500, lambda: window.grab().save(options.screenshot))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
