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
