"""
Project-aware directory system.
Provides per-script cache directories for input/output/logs/history.
"""

import os
import nuke

from ai_workflow.core.settings import app_settings, UNSAVED_PROJECT_DIR


def get_script_name():
    """Get the current Nuke script's base name (without path or extension).

    Returns UNSAVED_PROJECT_DIR for untitled scripts.
    """
    try:
        root_name = nuke.root().name()
        if root_name in ("", "root", "untitled", "Untitled"):
            return UNSAVED_PROJECT_DIR
        basename = os.path.basename(root_name)
        return os.path.splitext(basename)[0] or UNSAVED_PROJECT_DIR
    except Exception:
        return UNSAVED_PROJECT_DIR


def get_project_directory():
    """Get the cache directory for the CURRENT Nuke script/project.

    Returns: {project_cache_root}/{script_name}/
    """
    root = app_settings.project_cache_root
    script_name = get_script_name()
    proj_dir = os.path.join(root, script_name)
    if not os.path.exists(proj_dir):
        os.makedirs(proj_dir)
    return proj_dir


def get_temp_directory():
    """Get the temporary (project-aware) directory.

    DEPRECATED: Prefer get_project_directory() for new code.
    Kept for backward compatibility.
    """
    return get_project_directory()


def get_input_directory():
    """Get the input subdirectory inside project directory."""
    input_dir = os.path.join(get_project_directory(), "input")
    if not os.path.exists(input_dir):
        os.makedirs(input_dir)
    return input_dir


def get_output_directory():
    """Get the output subdirectory inside project directory."""
    output_dir = os.path.join(get_project_directory(), "output")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    return output_dir


def get_logs_directory():
    """Get the logs subdirectory inside project directory."""
    logs_dir = os.path.join(get_project_directory(), "logs")
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    return logs_dir
