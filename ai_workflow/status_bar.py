"""
Global Status Bar Progress Manager for AI Workflow.

Provides a persistent progress widget embedded in Nuke's main window status bar.
This widget survives node selection changes because it lives on the main window,
NOT inside any PyCustom_Knob widget.

Usage:
    from ai_workflow.status_bar import task_progress_manager
    
    # Register a task
    task_id = task_progress_manager.add_task("NanoBanana_Generate1", "image")
    
    # Update progress
    task_progress_manager.update_status(task_id, "Generating...", progress=50)
    
    # Complete / Error
    task_progress_manager.complete_task(task_id, "Done! Image saved.")
    task_progress_manager.error_task(task_id, "API Error")
"""

try:
    from PySide6 import QtWidgets, QtCore, QtGui
except ImportError:
    try:
        from PySide2 import QtWidgets, QtCore, QtGui
    except ImportError:
        from PySide import QtGui as QtWidgets
        from PySide import QtCore, QtGui

import nuke
import time


# ---------------------------------------------------------------------------
# Status Bar Widget (lives in Nuke's main window status bar)
# ---------------------------------------------------------------------------
class _TaskProgressWidget(QtWidgets.QWidget):
    """A compact widget showing generation progress for one or more tasks.
    Designed to sit in Nuke's bottom status bar area."""

    def __init__(self, parent=None):
        super(_TaskProgressWidget, self).__init__(parent)
        self.setObjectName("aiTaskProgress")
        self._tasks = {}  # task_id -> {label, progress, status, type, start_time}
        self._build_ui()
        self.setVisible(False)  # hidden until a task is registered

    def _build_ui(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)

        # AI icon/label
        self.icon_label = QtWidgets.QLabel("🤖")
        self.icon_label.setStyleSheet("font-size: 13px; background: transparent;")
        layout.addWidget(self.icon_label)

        # Task info label (shows task name + status text)
        self.info_label = QtWidgets.QLabel("")
        self.info_label.setStyleSheet(
            "color: #facc15; font-size: 11px; font-weight: bold; background: transparent;"
        )
        layout.addWidget(self.info_label)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedWidth(160)
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #555;
                border-radius: 4px;
                background-color: #333;
                text-align: center;
                color: #eee;
                font-size: 10px;
            }
            QProgressBar::chunk {
                background-color: #facc15;
                border-radius: 3px;
            }
        """)
        layout.addWidget(self.progress_bar)

        # Status text (detailed)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet(
            "color: #aaa; font-size: 10px; background: transparent;"
        )
        self.status_label.setMaximumWidth(300)
        layout.addWidget(self.status_label)

        # Cancel button (optional, for future use)
        # self.cancel_btn = QtWidgets.QPushButton("✕")
        # self.cancel_btn.setFixedSize(18, 18)
        # layout.addWidget(self.cancel_btn)

    def add_task(self, task_id, task_name, task_type="image"):
        """Register a new generation task."""
        self._tasks[task_id] = {
            "name": task_name,
            "type": task_type,
            "status": "Starting...",
            "progress": -1,  # -1 = indeterminate
            "start_time": time.time(),
        }
        self._refresh_display()
        self.setVisible(True)

    def update_status(self, task_id, status_text=None, progress=None):
        """Update the status text and/or progress (0-100) for a task."""
        if task_id not in self._tasks:
            return
        if status_text is not None:
            self._tasks[task_id]["status"] = status_text
        if progress is not None:
            self._tasks[task_id]["progress"] = progress
        self._refresh_display()

    def complete_task(self, task_id, message="Done!"):
        """Mark a task as completed and auto-hide after a delay."""
        if task_id not in self._tasks:
            return
        self._tasks[task_id]["status"] = message
        self._tasks[task_id]["progress"] = 100
        self._refresh_display()
        # Remove task after 5 seconds
        QtCore.QTimer.singleShot(5000, lambda: self._remove_task(task_id))

    def error_task(self, task_id, error_msg="Error"):
        """Mark a task as errored and auto-hide after a delay."""
        if task_id not in self._tasks:
            return
        self._tasks[task_id]["status"] = error_msg
        self._tasks[task_id]["progress"] = 0
        self._refresh_display_error()
        # Remove task after 8 seconds
        QtCore.QTimer.singleShot(8000, lambda: self._remove_task(task_id))

    def _remove_task(self, task_id):
        """Remove a task and hide if no tasks remain."""
        self._tasks.pop(task_id, None)
        if not self._tasks:
            self.setVisible(False)
            self.info_label.setText("")
            self.status_label.setText("")
            self.progress_bar.setValue(0)
            # Reset styles
            self.info_label.setStyleSheet(
                "color: #facc15; font-size: 11px; font-weight: bold; background: transparent;"
            )
        else:
            self._refresh_display()

    def _refresh_display(self):
        """Update the widget display based on current tasks."""
        if not self._tasks:
            self.setVisible(False)
            return

        active_count = len(self._tasks)

        if active_count == 1:
            task = list(self._tasks.values())[0]
            type_icon = "🖼️" if task["type"] == "image" else "🎬"
            self.icon_label.setText(type_icon)
            self.info_label.setText("[{}]".format(task["name"]))
            self.info_label.setStyleSheet(
                "color: #facc15; font-size: 11px; font-weight: bold; background: transparent;"
            )
            self.status_label.setText(task["status"])
            self.status_label.setStyleSheet(
                "color: #aaa; font-size: 10px; background: transparent;"
            )

            if task["progress"] < 0:
                # Indeterminate
                self.progress_bar.setRange(0, 0)
            else:
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(task["progress"])
        else:
            # Multiple tasks
            self.icon_label.setText("🤖")
            names = ", ".join(t["name"] for t in self._tasks.values())
            self.info_label.setText("{} tasks: {}".format(active_count, names))
            self.info_label.setStyleSheet(
                "color: #facc15; font-size: 11px; font-weight: bold; background: transparent;"
            )
            # Show aggregate progress (average)
            progresses = [t["progress"] for t in self._tasks.values() if t["progress"] >= 0]
            if progresses:
                avg = sum(progresses) // len(progresses)
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(avg)
            else:
                self.progress_bar.setRange(0, 0)

            statuses = ["{}: {}".format(t["name"], t["status"]) for t in self._tasks.values()]
            self.status_label.setText(" | ".join(statuses))
            self.status_label.setStyleSheet(
                "color: #aaa; font-size: 10px; background: transparent;"
            )

    def _refresh_display_error(self):
        """Update display with error styling."""
        self._refresh_display()
        self.info_label.setStyleSheet(
            "color: #ef4444; font-size: 11px; font-weight: bold; background: transparent;"
        )
        self.status_label.setStyleSheet(
            "color: #ef4444; font-size: 10px; background: transparent;"
        )


# ---------------------------------------------------------------------------
# Global Task Progress Manager (Singleton)
# ---------------------------------------------------------------------------
class TaskProgressManager(object):
    """Singleton manager that owns the status bar widget and provides
    a stable API for Worker threads to report progress.
    
    All public methods are thread-safe: they schedule UI updates on the
    main thread via nuke.executeInMainThread().
    """

    _instance = None
    _widget = None
    _installed = False
    _counter = 0  # for generating unique task IDs

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def install(self):
        """Install the progress widget into Nuke's main window status bar.
        Call this once from menu.py at startup."""
        if self._installed:
            print("[AI Status Bar] Already installed, skipping.")
            return

        try:
            main_window = self._find_nuke_main_window()
            if main_window is None:
                print("[AI Status Bar] Warning: Could not find Nuke main window")
                # DEBUG: list all top-level widgets for diagnostics
                for w in QtWidgets.QApplication.topLevelWidgets():
                    print("[AI Status Bar]   top-level widget: {} ({})".format(
                        w.objectName(), type(w).__name__))
                return

            print("[AI Status Bar] Found main window: {} ({})".format(
                main_window.objectName(), type(main_window).__name__))

            status_bar = main_window.statusBar()
            if status_bar is None:
                print("[AI Status Bar] Warning: No status bar found on main window")
                return

            print("[AI Status Bar] Found status bar: {} ({})".format(
                status_bar.objectName(), type(status_bar).__name__))

            self._widget = _TaskProgressWidget(parent=status_bar)
            # addPermanentWidget adds to the right side and stays visible
            status_bar.addPermanentWidget(self._widget)
            self._installed = True
            print("[AI Status Bar] Progress widget installed successfully!")
        except Exception as e:
            import traceback
            print("[AI Status Bar] Error installing widget: {}".format(e))
            traceback.print_exc()

    def _find_nuke_main_window(self):
        """Find Nuke's QMainWindow from the Qt application."""
        for widget in QtWidgets.QApplication.topLevelWidgets():
            if isinstance(widget, QtWidgets.QMainWindow):
                return widget
        return None

    def add_task(self, task_name, task_type="image"):
        """Add a new task and return its unique task_id.
        
        Args:
            task_name: Display name (e.g. node name)
            task_type: "image" or "video"
            
        Returns:
            task_id (str) for future update/complete/error calls.
        """
        self._counter += 1
        task_id = "task_{}_{:.0f}".format(self._counter, time.time())

        def _do():
            if self._widget:
                self._widget.add_task(task_id, task_name, task_type)
        nuke.executeInMainThread(_do)
        return task_id

    def update_status(self, task_id, status_text=None, progress=None):
        """Thread-safe status update."""
        def _do():
            if self._widget:
                self._widget.update_status(task_id, status_text, progress)
        nuke.executeInMainThread(_do)

    def complete_task(self, task_id, message="Done!"):
        """Thread-safe task completion."""
        def _do():
            if self._widget:
                self._widget.complete_task(task_id, message)
        nuke.executeInMainThread(_do)

    def error_task(self, task_id, error_msg="Error"):
        """Thread-safe task error."""
        def _do():
            if self._widget:
                self._widget.error_task(task_id, error_msg)
        nuke.executeInMainThread(_do)


# Module-level convenience accessor
task_progress_manager = TaskProgressManager.instance()


def deferred_install():
    """Call this from menu.py to install the status bar widget after Nuke startup.
    
    Usage in menu.py (single line, no indentation issues):
        import ai_workflow.status_bar; ai_workflow.status_bar.deferred_install()
    """
    import os as _os
    # DEBUG: write a marker file so we know this function was called
    _debug_path = _os.path.join(_os.path.expanduser("~"), ".nuke", "_status_bar_debug.txt")
    with open(_debug_path, "a") as _df:
        import datetime
        _df.write("deferred_install() called at {}\n".format(datetime.datetime.now()))

    def _do_install():
        try:
            # DEBUG: also log the deferred callback
            with open(_debug_path, "a") as _df2:
                import datetime as _dt2
                _df2.write("_do_install() executing at {}\n".format(_dt2.datetime.now()))
            task_progress_manager.install()
            # DEBUG: log result + show test task
            with open(_debug_path, "a") as _df3:
                _df3.write("install() finished. _installed={}, _widget={}\n".format(
                    task_progress_manager._installed, task_progress_manager._widget))
            # Show a brief test task so user can verify widget works at startup
            _test_tid = task_progress_manager.add_task("AI_Workflow", "image")
            task_progress_manager.update_status(_test_tid, "Status bar ready!", progress=100)
            # Auto-hide test task after 8 seconds
            QtCore.QTimer.singleShot(8000,
                lambda: task_progress_manager.complete_task(_test_tid, ""))
        except Exception as e:
            print("[AI Status Bar] deferred_install failed: {}".format(e))
            with open(_debug_path, "a") as _df4:
                import traceback
                _df4.write("ERROR: {}\n".format(e))
                traceback.print_exc(file=_df4)

    # Use QTimer.singleShot(0, ...) instead of nuke.executeDeferred()
    # because executeDeferred may not fire when called from menu.py during startup.
    # QTimer.singleShot(0) schedules the call for the next Qt event loop iteration,
    # which guarantees the QMainWindow and statusBar() are ready.
    QtCore.QTimer.singleShot(0, _do_install)
    with open(_debug_path, "a") as _df5:
        _df5.write("QTimer.singleShot(0, _do_install) registered\n")
