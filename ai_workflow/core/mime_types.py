"""统一 MIME 类型与文件支持策略。"""

import os

SUPPORTED_MIME_MAP = {
    # Images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    # PDF
    ".pdf": "application/pdf",
    # Microsoft Office
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # Text / structured
    ".txt": "text/plain",
    ".rtf": "application/rtf",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
}

INLINE_MIME_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".txt", ".csv", ".tsv", ".rtf",
}


def extension_of(path_or_ext):
    ext = (path_or_ext or "").lower().strip()
    if not ext:
        return ""
    if ext.startswith("."):
        return ext
    return os.path.splitext(ext)[1].lower()


def guess_mime_type(path_or_ext, default="image/png"):
    return SUPPORTED_MIME_MAP.get(extension_of(path_or_ext), default)


def is_supported_file(path_or_ext):
    return extension_of(path_or_ext) in SUPPORTED_MIME_MAP


def is_inline_file(path_or_ext):
    return extension_of(path_or_ext) in INLINE_MIME_EXTENSIONS
