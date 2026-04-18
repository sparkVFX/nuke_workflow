"""视频模型适配注册中心。

目标：接入新视频模型（如 Seedance）时，只需新增适配器并注册，
不需要在 UI/Worker 中复制一套请求拼装逻辑。
"""

_VIDEO_ADAPTERS = {}


class BaseVideoAdapter(object):
    ui_name = ""
    api_model = ""

    def build_generate_kwargs(self, prompt, mode, ref_images,
                              aspect_ratio=None, duration="8", resolution="720P",
                              types_module=None):
        raise NotImplementedError


class Veo31Adapter(BaseVideoAdapter):
    def __init__(self, ui_name, api_model):
        self.ui_name = ui_name
        self.api_model = api_model

    def build_generate_kwargs(self, prompt, mode, ref_images,
                              aspect_ratio=None, duration="8", resolution="720P",
                              types_module=None):
        if types_module is None:
            raise ValueError("types_module is required")

        config_kwargs = {}
        if aspect_ratio:
            config_kwargs["aspect_ratio"] = aspect_ratio

        # parse duration
        dur_seconds = 8
        if duration is not None:
            dur_str = str(duration).replace("s", "").strip()
            try:
                dur_val = int(dur_str)
                if 4 <= dur_val <= 8:
                    dur_seconds = dur_val
            except Exception:
                pass

        # API constraints
        if mode == "Frames":
            dur_seconds = 8
        if str(resolution or "").lower() in ("1080p", "4k"):
            dur_seconds = 8

        config_kwargs["duration_seconds"] = dur_seconds
        if resolution:
            config_kwargs["resolution"] = str(resolution).lower()

        generate_kwargs = {
            "model": self.api_model,
            "prompt": prompt,
        }

        has_refs = len(ref_images or []) > 0
        mode_str = "text-to-video"

        if mode == "Frames" and len(ref_images) >= 2:
            first_image = ref_images[0]
            last_image = ref_images[1]
            config_kwargs["last_frame"] = last_image
            generate_kwargs["image"] = first_image
            mode_str = "frames (first+last)"
        elif mode == "Frames" and len(ref_images) == 1:
            generate_kwargs["image"] = ref_images[0]
            mode_str = "frames (first only)"
        elif mode == "FirstFrame" and len(ref_images) >= 1:
            generate_kwargs["image"] = ref_images[0]
            mode_str = "first-frame"
        elif has_refs:
            config_kwargs["reference_images"] = [
                types_module.VideoGenerationReferenceImage(
                    image=ri, reference_type="asset"
                ) for ri in ref_images
            ]
            mode_str = "ref-to-video ({} refs)".format(len(ref_images))

        config = types_module.GenerateVideosConfig(**config_kwargs)
        generate_kwargs["config"] = config
        return generate_kwargs, config_kwargs, mode_str, dur_seconds


def register_video_adapter(adapter):
    if not adapter or not getattr(adapter, "ui_name", ""):
        return
    _VIDEO_ADAPTERS[adapter.ui_name] = adapter


def get_video_adapter(ui_name):
    if ui_name in _VIDEO_ADAPTERS:
        return _VIDEO_ADAPTERS[ui_name]
    # fallback: allow passing api model id directly
    for ad in _VIDEO_ADAPTERS.values():
        if getattr(ad, "api_model", None) == ui_name:
            return ad
    return _VIDEO_ADAPTERS.get("Google VEO 3.1-Fast")


def list_video_adapters():
    return list(_VIDEO_ADAPTERS.keys())


def resolve_video_model_id(ui_name):
    adapter = get_video_adapter(ui_name)
    if adapter:
        return adapter.api_model
    return ui_name


# 默认注册 VEO 3.1 系列
register_video_adapter(Veo31Adapter("Google VEO 3.1-Fast", "veo-3.1-fast-generate-preview"))
register_video_adapter(Veo31Adapter("Google VEO 3.1", "veo-3.1-generate-preview"))
DEFAULT_VIDEO_MODEL_UI = "Google VEO 3.1-Fast"
