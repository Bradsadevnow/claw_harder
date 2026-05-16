from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

QT_BINDING = None
try:
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtWidgets import (
        QApplication,
        QLabel,
        QListWidget,
        QMainWindow,
        QSplitter,
        QTextEdit,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    QT_BINDING = "PyQt5"
except ImportError:
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import (
            QApplication,
            QLabel,
            QListWidget,
            QMainWindow,
            QSplitter,
            QTextEdit,
            QTreeWidget,
            QTreeWidgetItem,
            QVBoxLayout,
            QWidget,
        )

        QT_BINDING = "PySide6"
    except ImportError:
        QT_BINDING = None

if QT_BINDING is None:
    class _QtStub:
        UserRole = 0
        Horizontal = 1
        Vertical = 2

    class _WidgetStub:
        def __init__(self, *args, **kwargs):
            pass

    Qt = _QtStub()
    QTimer = _WidgetStub
    QApplication = QLabel = QListWidget = QMainWindow = QSplitter = QTextEdit = QTreeWidget = QTreeWidgetItem = QVBoxLayout = QWidget = _WidgetStub


def panel_is_available() -> bool:
    return QT_BINDING is not None


def parse_event_record(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def format_event_summary(record: dict[str, Any]) -> str:
    cycle = record.get("cycle", "?")
    seq = record.get("seq", "?")
    module = record.get("module")
    kind = record.get("kind") or record.get("event") or "event"
    level = record.get("level")
    msg = str(record.get("msg", "")).strip()

    label = f"[{cycle}:{seq}]"
    if module:
        label += f" {module}:{kind}"
    else:
        label += f" {kind}"
    if level and level != "info":
        label += f" <{level}>"
    if msg:
        label += f" {msg}"
    return label


def read_status_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class FileExplorer(QTreeWidget):
    def __init__(self, workspace_root: Path, viewer: "ContentViewer"):
        super().__init__()
        self.workspace_root = workspace_root
        self.viewer = viewer
        self.setHeaderLabel("Workspace")
        self.itemClicked.connect(self.open_item)
        self.populate()

    def populate(self) -> None:
        self.clear()
        root = QTreeWidgetItem(self, [self.workspace_root.name or str(self.workspace_root)])
        root.setData(0, Qt.UserRole, str(self.workspace_root))
        self._add_items(root, self.workspace_root)
        self.expandToDepth(1)

    def _add_items(self, parent: QTreeWidgetItem, path: Path) -> None:
        try:
            children = sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        except OSError:
            return
        for child in children:
            item = QTreeWidgetItem(parent, [child.name + ("/" if child.is_dir() else "")])
            item.setData(0, Qt.UserRole, str(child))
            if child.is_dir():
                self._add_items(item, child)

    def open_item(self, item: QTreeWidgetItem) -> None:
        raw = item.data(0, Qt.UserRole)
        path = Path(str(raw))
        if path.is_file():
            self.viewer.load_file(path)


class ContentViewer(QTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setPlaceholderText("Select a file to view.")

    def load_file(self, path: Path) -> None:
        try:
            self.setPlainText(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            self.setPlainText(f"[ERROR] {path} is not valid UTF-8 text.")
        except OSError as exc:
            self.setPlainText(f"[ERROR] {exc}")


class EventStream(QListWidget):
    def __init__(self, log_path: Path):
        super().__init__()
        self.log_path = log_path
        self._offset = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self._timer.start(500)
        self.addItem(f"Watching {self.log_path}")
        self._file = None
        self._open_log()

    def _open_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        self._file = self.log_path.open("r", encoding="utf-8")
        self._file.seek(0, os.SEEK_END)
        self._offset = self._file.tell()

    def poll(self) -> None:
        if self._file is None:
            self._open_log()
        assert self._file is not None
        self._file.seek(self._offset)
        while True:
            line = self._file.readline()
            if not line:
                break
            self._offset = self._file.tell()
            record = parse_event_record(line)
            if record is None:
                self.addItem(line.strip())
            else:
                self.addItem(format_event_summary(record))
            self.scrollToBottom()


class HealthHUD(QWidget):
    def __init__(self, status_path: Path):
        super().__init__()
        self.status_path = status_path
        layout = QVBoxLayout()
        self.motto = QLabel("State is derived. Nothing is trusted. Everything is replayable.")
        self.binding = QLabel(f"Binding: {QT_BINDING or 'missing'}")
        self.global_status = QLabel("Status: --")
        self.stage = QLabel("Stage: --")
        self.mode = QLabel("Governance: --")
        self.execution = QLabel("Execution: --")
        self.cycle = QLabel("Cycle: --")
        self.memory = QLabel("Memory Frames: --")
        self.nodes = QLabel("Identity Nodes (confirmed/provisional): --")
        self.drift = QLabel("Drift Ratio: --")
        self.pending = QLabel("Tick Mode: off")
        self.killswitch = QLabel("Emergency Stop: --")
        self.issue_banner = QLabel("Issues: none")
        self.pending_queue = QLabel("Tick Count: --")
        self.required_action = QLabel("Action: none")
        for widget in (
            self.motto,
            self.binding,
            self.global_status,
            self.stage,
            self.mode,
            self.execution,
            self.cycle,
            self.memory,
            self.nodes,
            self.drift,
            self.pending,
            self.killswitch,
            self.issue_banner,
            self.pending_queue,
            self.required_action,
        ):
            if hasattr(widget, "setWordWrap"):
                widget.setWordWrap(True)
            layout.addWidget(widget)
        layout.addStretch()
        self.setLayout(layout)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update_status)
        self._timer.start(1000)
        self.update_status()

    def update_status(self) -> None:
        payload = read_status_file(self.status_path)
        snapshot = payload.get("status_snapshot", {}) if isinstance(payload.get("status_snapshot"), dict) else {}
        health = payload.get("health_signal", {}) if isinstance(payload.get("health_signal"), dict) else {}
        tick_mode = str(payload.get("tick_mode", "off"))
        execution_mode = str(payload.get("execution_mode", "plan"))

        status_label = str(health.get("status", payload.get("health_status", "unknown")))
        self.global_status.setText(f"Status: {status_label}")
        self.stage.setText(f"Stage: {payload.get('stage', '--')}")
        self.mode.setText(f"Governance: {payload.get('governance_mode', '--')}")
        self.execution.setText(f"Execution: {execution_mode}")
        self.cycle.setText(f"Cycle: {snapshot.get('cycle', health.get('cycle', '--'))}")
        self.memory.setText(f"Memory Frames: {snapshot.get('memory_frames', '--')}")
        active_nodes = int(health.get("active_nodes", 0) or 0)
        provisional_nodes = int(health.get("provisional_nodes", 0) or 0)
        total = active_nodes + provisional_nodes
        ratio = (provisional_nodes / total) if total > 0 else 0.0
        self.nodes.setText(f"Identity Nodes (confirmed/provisional): {active_nodes} / {provisional_nodes}")
        self.drift.setText(f"Drift Ratio: {ratio:.2f} (threshold {health.get('drift_threshold', '--')})")
        self.pending.setText(f"Tick Mode: {tick_mode}")
        self.pending_queue.setText(f"Tick Count: {snapshot.get('tick_count', health.get('tick_count', '--'))}")
        killswitch_reason = str(payload.get("killswitch_reason", "") or health.get("killswitch_reason", ""))
        killswitch_engaged = bool(payload.get("killswitch_engaged", health.get("killswitch_engaged", False)))
        if killswitch_engaged:
            label = "Emergency Stop: ENGAGED"
            if killswitch_reason:
                label += f" ({killswitch_reason})"
            self.killswitch.setText(label)
        else:
            self.killswitch.setText("Emergency Stop: clear")
        issue_reasons = health.get("issue_reasons", payload.get("issue_reasons", []))
        if not isinstance(issue_reasons, list):
            issue_reasons = []
        issue = bool(health.get("issue", payload.get("issue", False)))
        self.issue_banner.setText(f"Issues: {', '.join(issue_reasons) if issue_reasons else 'none'}")
        if issue and killswitch_engaged:
            self.required_action.setText("Action: release emergency stop when safe to continue.")
        elif issue:
            self.required_action.setText("Action: inspect issue reasons in trace and resolve root cause.")
        else:
            self.required_action.setText("Action: none")


class RuntimePanel(QMainWindow):
    def __init__(self, workspace_root: Path, log_path: Path, status_path: Path):
        super().__init__()
        self.setWindowTitle("RUNTIME PANEL")
        self.setGeometry(100, 100, 1400, 900)

        self.viewer = ContentViewer()
        self.explorer = FileExplorer(workspace_root, self.viewer)
        self.events = EventStream(log_path)
        self.hud = HealthHUD(status_path)

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.addWidget(self.explorer)
        top_splitter.addWidget(self.viewer)
        top_splitter.addWidget(self.hud)
        top_splitter.setSizes([300, 800, 300])

        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.addWidget(top_splitter)
        main_splitter.addWidget(self.events)
        main_splitter.setSizes([720, 180])

        container = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(main_splitter)
        container.setLayout(layout)
        self.setCentralWidget(container)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runtime observer panel")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--status-path", required=True)
    return parser


def launch_panel(workspace_root: Path, log_path: Path, status_path: Path) -> int:
    if QT_BINDING is None:
        print(
            "Runtime panel requires PyQt5 or PySide6. Install one of them to launch the observer window.",
            file=sys.stderr,
        )
        return 2

    app = QApplication(sys.argv)
    window = RuntimePanel(workspace_root, log_path, status_path)
    window.show()
    return app.exec_()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return launch_panel(Path(args.workspace), Path(args.log_path), Path(args.status_path))


if __name__ == "__main__":
    raise SystemExit(main())
