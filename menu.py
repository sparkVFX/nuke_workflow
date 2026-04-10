"""
Nuke menu.py - Entry point for AI Workflow plugin.
Copy this file to C:/Users/chen_/.nuke/menu.py
or append its content to the existing menu.py in that directory.
"""

import ai_workflow.toolbar

# Register the AI Workflow toolbar in the left sidebar
ai_workflow.toolbar.register_toolbar()
