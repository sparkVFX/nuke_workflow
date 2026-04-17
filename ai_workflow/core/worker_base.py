"""
Base worker thread class and active worker registry.
Shared by NanoBanana and VEO workers.
"""

from ai_workflow.core.pyside_compat import QtCore

# Module-level registry for active workers.
# Prevents garbage collection when the Widget is destroyed mid-generation.
_active_workers = {}


def register_active_worker(worker_id, worker, params=None):
    """Register an active worker to prevent GC."""
    _active_workers[worker_id] = {"worker": worker, "params": params or {}}


def unregister_active_worker(worker_id):
    """Remove a worker from the active registry."""
    _active_workers.pop(worker_id, None)


class BaseWorker(QtCore.QThread):
    """Base class for AI generation worker threads.

    Subclasses must override _execute() with their specific API logic.
    Emits finished(success, result_or_error) when done.
    """

    finished = QtCore.Signal(bool, object)

    def __init__(self, callback=None, parent=None):
        super(BaseWorker, self).__init__(parent)
        self._callback = callback
        self._is_running = False

    @property
    def is_running(self):
        return self._is_running

    def run(self):
        self._is_running = True
        try:
            result = self._execute()
            self._is_running = False
            if self._callback:
                self._callback(True, result)
            self.finished.emit(True, result)
        except Exception as e:
            self._is_running = False
            if self._callback:
                self._callback(False, str(e))
            self.finished.emit(False, str(e))

    def _execute(self):
        """Override this method in subclasses with the actual work."""
        raise NotImplementedError("Subclasses must implement _execute()")

    def stop(self):
        """Request the worker to stop. Override for custom cancellation."""
        self._is_running = False
