"""
ai_workflow.core — Shared modules for AI Workflow Nuke plugin.

Extracted from nanobanana.py, veo.py, and gemini_chat.py to enable
code reuse across multiple AI model integrations (Gemini, Veo, Seedance, etc.).
"""

from ai_workflow.core.pyside_compat import QtWidgets, QtCore, QtGui, _isValid

from ai_workflow.core.ui_components import DropDownComboBox, SHARED_DARK_STYLE
from ai_workflow.core.model_catalog import (
    NB_MODEL_OPTIONS,
    NB_RATIO_OPTIONS,
    NB_RESOLUTION_OPTIONS,
    VEO_MODEL_OPTIONS,
    VEO_RATIO_OPTIONS,
    VEO_RESOLUTION_OPTIONS,
    VEO_DURATION_OPTIONS,
    VEO_MODE_OPTIONS,
    CHAT_MODELS,
    fill_combo_from_options,
)
from ai_workflow.core.mime_types import (
    SUPPORTED_MIME_MAP,
    INLINE_MIME_EXTENSIONS,
    guess_mime_type,
)
from ai_workflow.core.history_store import (
    get_history,
    set_history,
    push_history_item,
)
from ai_workflow.core.video_model_registry import (
    register_video_adapter,
    get_video_adapter,
    list_video_adapters,
    resolve_video_model_id,
    DEFAULT_VIDEO_MODEL_UI,
)


from ai_workflow.core.settings import (
    AppSettings,
    app_settings,
    CONFIG_FILE_NAME,
    DEFAULT_TEMP_DIR_NAME,
    DEFAULT_PROJECT_CACHE_NAME,
    UNSAVED_PROJECT_DIR,
)

from ai_workflow.core.directories import (
    get_script_name,
    get_project_directory,
    get_temp_directory,
    get_input_directory,
    get_output_directory,
    get_logs_directory,
)

from ai_workflow.core.rendering import (
    render_input_to_file_silent,
    collect_input_images,
    collect_input_image_paths,
)

from ai_workflow.core.api_helpers import (
    image_to_base64,
    get_mime_type,
    call_gemini_api,
    extract_image_from_response,
)

from ai_workflow.core.nuke_utils import (
    get_internal_read,
    next_node_name,
    rebuild_group_for_thumbnail,
    update_node_thumbnail,
    restore_thumbnails,
)

from ai_workflow.core.worker_base import (
    BaseWorker,
    register_active_worker,
    unregister_active_worker,
)
