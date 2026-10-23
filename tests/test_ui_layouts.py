"""Layout, theme and start-page behaviour on real projects.

These tests use the real project store and background workers. Nothing here
replaces the A-module acceptance: they prove the three approved layouts share
one working window and one data path.
"""

import pytest
from openpyxl import Workbook
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea

from traceatlas.project import Project
from traceatlas.ui.main_window import MainWindow
from traceatlas.ui.settings_dialog import SettingsDialog
from traceatlas.ui.theme import LAYOUTS, tokens


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    settings = QSettings("TraceAtlas", "TraceAtlas")
    settings.clear()
    settings.sync()


@pytest.fixture
def workbook(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.title = "流水明细"
    sheet.append(["本方账户", "交易金额", "对方账户", "交易日期"])
    sheet.append(["00123", "100.50", "98765", "2026-10-01"])
    sheet.append(["00123", "20.25", "98766", "2026-10-02"])
    sheet.append(["00123", "30.00", "98767", "2026-10-03"])
    sheet.append(["00999", "40.00", "98767", "2026-10-04"])
    path = tmp_path / "布局流水.xlsx"
    book.save(path)
    return path


def import_dataset(qtbot, window, workbook):
    parameters = {
        "path": str(workbook),
        "sheet": "流水明细",
        "header_row": 1,
        "start_row": 2,
        "mapping": {"owner_account": 1, "amount": 2, "counterparty_account": 3, "occurred_at": 4},
        "platform": "bank",
        "account_namespace": "account",
        "currency": "CNY",
        "timezone": "unknown",
        "allow_partial": True,
    }
    task = window.submit_task("xlsx.import", parameters)
    assert task
    qtbot.waitUntil(
        lambda: window.project.get_task(task)["state"] in {"succeeded", "failed"}, timeout=30000
    )
    assert window.project.get_task(task)["state"] == "succeeded"
    window.poll_tasks()
    return window.project.objects("dataset")[0]


def test_home_entries_are_real_in_every_layout(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    recent = tmp_path / "已有项目"
    recent.mkdir()
    window.settings.setValue("projects/recent", [str(recent)])
    window._refresh_home()
    for layout in LAYOUTS:
        window.set_layout(layout)
        new, open_, continue_, import_ = window.home.entries()
        assert new.isEnabled() and open_.isEnabled(), layout
        assert continue_.isEnabled(), f"{layout}: continue must open the last project"
        assert not import_.isVisible(), f"{layout}: import needs a project"
        assert window.home.stack.currentWidget() is window.home.views[layout]
        assert window.home.layout_buttons[layout].isChecked()
    window.settings.setValue("projects/recent", [])
    window._refresh_home()
    assert not window.home.entries()[2].isEnabled(), "continue is disabled without a recent project"
    window.close()


def test_layout_and_theme_persist_for_the_next_start(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_layout("network")
    window.set_theme("dark")
    window.set_motion_paused(True)
    window.close()
    reopened = MainWindow()
    qtbot.addWidget(reopened)
    assert reopened.layout_id == "network"
    assert reopened.theme_setting == "dark"
    assert reopened.motion_paused is True
    assert reopened._scheme.mode == "dark"
    assert reopened.home.layout == "network"
    reopened.close()


def test_layout_switch_keeps_project_filters_selection_and_tasks(qtbot, workbook, tmp_path):
    project = Project.create(tmp_path / "切换保持", "切换保持")
    window = MainWindow(project)
    qtbot.addWidget(window)
    window.show()
    dataset = import_dataset(qtbot, window, workbook)
    window.show_object(dataset["id"])
    qtbot.waitUntil(lambda: window._query_ready, timeout=20000)
    window.filter_column.setCurrentIndex(window.filter_column.findData("owner_account"))
    window.filter_operator.setCurrentIndex(window.filter_operator.findData("eq"))
    window.filter_value.setText("00123")
    qtbot.mouseClick(window.filter_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._query_ready and window.total == 3, timeout=20000)
    selected = window.table.selectionModel().selectedRows()[0].row()
    task_count = window.tasks_table.topLevelItemCount()
    expected_row = window.table_model.rows[selected]
    expected_amount = window.inspector.amount_label.text()
    for layout in ("terminal", "network", "folio"):
        window.set_layout(layout)
        assert window.project is project
        assert window.current_object["id"] == dataset["id"]
        assert window.filters == [{"column": "owner_account", "op": "eq", "value": "00123"}]
        assert window.total == 3
        assert window.workspace.currentWidget() is window.data_page
        assert window.table.selectionModel().selectedRows()[0].row() == selected
        assert window.tasks_table.topLevelItemCount() == task_count
        for theme in ("dark", "light", "system"):
            window.set_theme(theme)
            window.workspace.setCurrentWidget(window.index_page)
            window.workspace.setCurrentWidget(window.data_page)
            assert window.inspector._row == expected_row
            assert window.inspector.amount_label.text() == expected_amount
            assert window.inspector.fields_host.isVisible()
    window.close()
    project.close()


def test_continue_project_opens_the_last_project(qtbot, tmp_path):
    created = Project.create(tmp_path / "继续上次", "继续上次")
    first = MainWindow(created)
    qtbot.addWidget(first)
    first.close()
    second = MainWindow()
    qtbot.addWidget(second)
    second.show()
    assert second.home.entries()[2].isEnabled()
    qtbot.mouseClick(second.home.entries()[2], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: second.project is not None, timeout=20000)
    assert second.project.path == created.path
    assert second.project_label.text().startswith("继续上次")
    second.close()


def test_network_entries_are_not_covered_by_the_canvas(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1280, 820)
    window.show()
    window.set_layout("network")
    qtbot.waitUntil(lambda: window.home.views["network"].isVisible(), timeout=5000)
    view = window.home.views["network"]
    canvas = view.canvas
    canvas_rect = canvas.rect().translated(canvas.mapTo(window, QPoint(0, 0)))
    for button in (view.new_button, view.open_button, view.continue_button):
        assert button.isVisible()
        rect = button.rect().translated(button.mapTo(window, QPoint(0, 0)))
        assert not rect.intersects(canvas_rect), f"{button.text()} overlaps the decorative canvas"
        assert rect.width() > 80 and rect.height() > 20
    window.close()


def test_motion_pause_stops_the_decorative_canvas(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_layout("network")
    window.show()
    canvas = window.home.views["network"].canvas
    window.set_motion_paused(False)
    assert canvas._timer.isActive()
    window.set_motion_paused(True)
    assert not canvas._timer.isActive()
    assert window.motion_action.isChecked()
    window.set_motion_paused(False)
    window.set_layout("folio")
    assert not canvas._timer.isActive(), "hidden canvas must not repaint in the background"
    window.set_layout("network")
    assert canvas._timer.isActive()
    window.close()


def test_settings_dialog_applies_live_and_writes_each_choice(qtbot, tmp_path):
    settings = QSettings("TraceAtlas", "TraceAtlas")
    dialog = SettingsDialog("folio", "system", False, settings)
    qtbot.addWidget(dialog)
    changes = {"layout": [], "theme": [], "motion": []}
    dialog.layoutChanged.connect(lambda value: changes["layout"].append(value))
    dialog.themeChanged.connect(lambda value: changes["theme"].append(value))
    dialog.motionChanged.connect(lambda value: changes["motion"].append(value))
    for button in dialog.layout_buttons.buttons():
        if button.text() == "金融终端":
            button.setChecked(True)
    assert changes["layout"] == ["terminal"]
    assert settings.value("ui/layout") == "terminal"
    for button in dialog.theme_buttons.buttons():
        if button.text() == "暗":
            button.setChecked(True)
    assert changes["theme"] == ["dark"]
    assert settings.value("ui/theme") == "dark"
    dialog.motion_box.setChecked(True)
    assert changes["motion"] == [True]
    assert str(settings.value("ui/motion_paused")).lower() in {"true", "1"}
    dialog.accept()


def test_terminal_instrument_shows_real_page_values(qtbot, workbook, tmp_path):
    project = Project.create(tmp_path / "仪表", "仪表")
    window = MainWindow(project)
    qtbot.addWidget(window)
    window.show()
    window.set_layout("terminal")
    dataset = import_dataset(qtbot, window, workbook)
    window.show_object(dataset["id"])
    qtbot.waitUntil(lambda: window._query_ready, timeout=20000)
    assert window.instrument.values["records"].text() == f"{window.total:,}"
    assert window.instrument.values["page"].text() == "1/1"
    assert len(window.instrument.sparkline._values) == len(window.table_model.rows)
    assert window.instrument.sparkline._values, "real page amounts feed the signal line"
    window.close()
    project.close()


def test_theme_changes_colour_not_layout(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_layout("folio")
    before = window.page_tabs.objectName()
    window.set_theme("dark")
    dark = tokens("folio", "dark")
    assert window._scheme.bg == dark.bg
    assert window.page_tabs.objectName() == before
    application_stylesheet = QApplication.instance().styleSheet()
    assert dark.surface in application_stylesheet, "dark token must reach the application stylesheet"
    window.set_theme("light")
    assert window._scheme.mode == "light"
    window.close()


def test_running_task_survives_layout_switches(qtbot, workbook, tmp_path):
    project = Project.create(tmp_path / "任务保持", "任务保持")
    window = MainWindow(project)
    qtbot.addWidget(window)
    window.show()
    parameters = {
        "path": str(workbook),
        "sheet": "流水明细",
        "header_row": 1,
        "start_row": 2,
        "mapping": {"owner_account": 1, "amount": 2, "counterparty_account": 3, "occurred_at": 4},
        "platform": "bank",
        "account_namespace": "account",
        "currency": "CNY",
        "timezone": "unknown",
        "allow_partial": True,
    }
    task = window.submit_task("xlsx.import", parameters)
    assert task
    for layout in ("terminal", "network", "folio"):
        window.set_layout(layout)
        assert window.manager is not None and window.project is project
    qtbot.waitUntil(
        lambda: project.get_task(task)["state"] in {"succeeded", "failed"}, timeout=30000
    )
    assert project.get_task(task)["state"] == "succeeded"
    window.poll_tasks()
    assert window.tasks_table.topLevelItemCount() == 1
    assert [obj["kind"] for obj in project.objects()].count("dataset") == 1
    window.close()
    project.close()


def scroll_ancestor(widget):
    parent = widget.parent()
    while parent is not None and not isinstance(parent, QScrollArea):
        parent = parent.parent()
    return parent


def test_short_window_keeps_entries_reachable(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1000, 680)
    window.show()
    window.set_layout("terminal")
    qtbot.waitUntil(lambda: window.home.views["terminal"].isVisible(), timeout=5000)
    for layout in ("terminal", "folio", "network"):
        window.set_layout(layout)
        view = window.home.views[layout]
        assert scroll_ancestor(view.new_button) is not None, f"{layout} entries must stay reachable"
        assert view.new_button.isVisible()
    window.close()


def test_home_search_shortcut_filters_recent_projects(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    for name in ("甲项目", "乙项目"):
        (tmp_path / name).mkdir()
    window.settings.setValue("projects/recent", [str(tmp_path / "甲项目"), str(tmp_path / "乙项目")])
    window._refresh_home()
    window.focus_home_search()
    search = window.home.active_view.search
    assert window.focusWidget() is search
    search.setText("乙")
    rows = [
        row
        for row in window.home.active_view.list_host.findChildren(QPushButton)
        if row.objectName() == "recentRow"
    ]
    assert len(rows) == 1, "the search line filters the real recent-project rows"
    assert "乙项目" in rows[0].name_label.text()
    search.clear()
    rows = [
        row
        for row in window.home.active_view.list_host.findChildren(QPushButton)
        if row.objectName() == "recentRow"
    ]
    assert len(rows) == 2
    window.close()


def test_short_workspace_keeps_rows_paging_and_source_fields_reachable(qtbot, workbook, tmp_path):
    project = Project.create(tmp_path / "短窗口", "短窗口")
    window = MainWindow(project)
    qtbot.addWidget(window)
    window.show()
    dataset = import_dataset(qtbot, window, workbook)
    window.show_object(dataset["id"])
    qtbot.waitUntil(lambda: window._query_ready, timeout=20000)
    window.resize(1024, 700)
    for layout in LAYOUTS:
        window.set_layout(layout)
        for theme in ("light", "dark"):
            window.set_theme(theme)
            QApplication.processEvents()
            assert window.width() == 1024 and window.height() == 700
            assert window.table.geometry().bottom() < window.previous_page.geometry().top()
            window.data_page.ensureWidgetVisible(window.previous_page)
            QApplication.processEvents()
            assert window.previous_page.visibleRegion().boundingRect() == window.previous_page.rect()
            assert window.table.viewport().visibleRegion().boundingRect().height() >= 50
            inspector = window.inspector
            last_field = inspector.value_labels["record_id"]
            inspector.details_scroll.ensureWidgetVisible(last_field)
            QApplication.processEvents()
            assert last_field.visibleRegion().boundingRect() == last_field.rect()
            assert last_field.isReadOnly()
            assert last_field.toPlainText() == str(window._current_row()["record_id"])
            assert last_field.height() >= last_field.document().size().height()
            assert inspector.fields_host.height() >= inspector.grid.minimumSize().height()
            assert inspector.sources_button.visibleRegion().boundingRect() == inspector.sources_button.rect()
            assert inspector.sources_button.isEnabled()
            if layout == "terminal":
                curve = window.instrument.sparkline
                assert curve.visibleRegion().boundingRect() == curve.rect()
    window.close()
    project.close()
