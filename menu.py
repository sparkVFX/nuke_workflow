"""
Nuke menu.py - Entry point for AI Workflow plugin.
Copy this file to C:/Users/chen_/.nuke/menu.py
or append its content to the existing menu.py in that directory.
"""

import ai_workflow.toolbar

# Register the AI Workflow toolbar in the left sidebar
ai_workflow.toolbar.register_toolbar()

# Register Gemini Chat as a dockable Nuke panel (appears as a tab like Properties / Scene Graph)
try:
    import ai_workflow.gemini_chat
    ai_workflow.gemini_chat.register_gemini_panel()
except Exception as _e:
    print("[AI Workflow] Could not register Gemini panel: {}".format(_e))

# Register Media Browser as a dockable Nuke panel
try:
    import ai_workflow.media_browser
    nukescripts.panels.registerWidgetAsPanel(
        "ai_workflow.media_browser._create_media_browser_widget",
        "Media Library",
        "ai_workflow.MediaBrowserPanel",
    )
except Exception as _e:
    print("[AI Workflow] Could not register Media Browser panel: {}".format(_e))

# Install the global status bar progress widget (shows generation progress at the bottom
# of Nuke's main window, independent of node selection).
# We defer the install slightly so the main window is fully initialized.
try:
    from ai_workflow.status_bar import task_progress_manager

    def _deferred_install_status_bar():
        try:
            task_progress_manager.install()
            # ===== DEBUG: Show a fixed test string to verify the widget is working =====
            _test_id = task_progress_manager.add_task("DEBUG_TEST", "image")
            task_progress_manager.update_status(_test_id, "Hello! Status bar is working!", progress=66)
            print("[AI Workflow] DEBUG: Test task added to status bar")
            # ===========================================================================
        except Exception as _e2:
            print("[AI Workflow] Could not install status bar widget: {}".format(_e2))

    # Use nuke.executeDeferred (alias for "run after Nuke startup completes")
    # so the QMainWindow and its statusBar() are guaranteed to exist.
    import nuke
    nuke.executeDeferred(_deferred_install_status_bar)
except Exception as _e:
    print("[AI Workflow] Could not prepare status bar: {}".format(_e))
