"""
共享模型目录与 UI 选项。

把 NanoBanana / VEO / Gemini Chat 的模型与参数选项集中管理，
避免在多个面板里重复硬编码。
"""

# NanoBanana
NB_MODEL_OPTIONS = [
    ("Gemini 3.1 Flash - Nano Banana 2", "gemini-3.1-flash-image-preview"),
    ("Gemini 3 Pro - Nano Banana Pro", "gemini-3-pro-image-preview"),
    ("Gemini 2.5 Flash - Nano Banana", "gemini-2.5-flash-image"),
    ("Gemini 2.0 Flash Exp (Image Gen)", "gemini-2.0-flash-exp-image-generation"),
    ("Imagen 3.0 Generate", "imagen-3.0-generate-002"),
]
NB_RATIO_OPTIONS = ["Auto", "1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "4:5"]
NB_RESOLUTION_OPTIONS = [("1K", "1K"), ("2K", "2K"), ("4K", "4K")]

# VEO
VEO_MODEL_OPTIONS = [
    ("Google VEO 3.1-Fast", "veo-3.1-fast-generate-preview"),
    ("Google VEO 3.1", "veo-3.1-generate-preview"),
]
VEO_RATIO_OPTIONS = ["16:9", "9:16"]
VEO_RESOLUTION_OPTIONS = ["720P", "1080P"]
VEO_DURATION_OPTIONS = [("4", "4"), ("6", "6"), ("8", "8")]
VEO_MODE_OPTIONS = [
    ("Text", "Text"),
    ("FirstFrame", "FirstFrame"),
    ("Frames", "Frames"),
    ("Ingredients", "Ingredients"),
]

# Seedance (Volcengine Ark API)
SEEDANCE_MODEL_OPTIONS = [
    ("Seedance 2.0 (默认)", "doubao-seedance-2-0-260128"),
    ("Seedance 1.5 Pro", "doubao-seedance-1-5-pro-251215"),
    ("Seedance 1.0 Pro", "doubao-seedance-1-0-pro-250528"),
    ("Seedance 1.0 Pro Fast", "doubao-seedance-1-0-pro-fast-251015"),
]
SEEDANCE_RATIO_OPTIONS = ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"]
SEEDANCE_DURATION_OPTIONS = [("4", 4), ("5", 5), ("8", 8), ("10", 10), ("15", 15), ("Auto(-1)", -1)]
SEEDANCE_RESOLUTION_OPTIONS = ["480p", "720p", "1080p"]
SEEDANCE_MODE_OPTIONS = [
    ("Text(文本生视频)", "text"),
    ("Image(首帧图生视频)", "image"),
    ("Frames(首尾帧图生视频)", "frames"),
    ("OmniRef(全能参考-多素材)", "omni_reference"),
    ("VideoExtend(视频延长/续写)", "video_extend"),
    ("AudioDrive(音频驱动)", "audio_drive"),
]

# Gemini Chat
CHAT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]


def fill_combo_from_options(combo, options, clear=True):
    """把 options 写入 QComboBox。

    options 支持两种格式：
    - ["A", "B"]
    - [("显示名", "值"), ...]
    """
    if clear:
        combo.clear()
    for item in options:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            combo.addItem(item[0], item[1])
        else:
            combo.addItem(str(item))
