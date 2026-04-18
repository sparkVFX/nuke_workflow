## AI Workflow 模块化架构说明

本次重构将原先分散在 `nanobanana.py`、`veo.py`、`gemini_chat.py` 的通用能力下沉到 `ai_workflow/core`，目标是：

- 减少重复代码
- 统一接口
- 新模型（如 Seedance）可插拔接入

### 新增通用模块

- `core/model_catalog.py`
  - 统一管理模型与参数选项（NB / VEO / Chat）
  - 提供 `fill_combo_from_options(combo, options)` 统一填充下拉框

- `core/mime_types.py`
  - 统一 MIME 映射与文件类型策略
  - 核心接口：
    - `guess_mime_type(path_or_ext, default)`
    - `is_supported_file(path_or_ext)`
    - `is_inline_file(path_or_ext)`

- `core/history_store.py`
  - 统一历史记录存储（支持 `project` / `global`）
  - 核心接口：
    - `get_history(key, scope="project", limit=20)`
    - `set_history(key, values, scope="project", limit=20)`
    - `push_history_item(key, item, scope="project", limit=20)`

- `core/video_model_registry.py`
  - 视频模型适配器注册中心（面向扩展）
  - 核心接口：
    - `register_video_adapter(adapter)`
    - `get_video_adapter(ui_name)`
    - `resolve_video_model_id(ui_name)`
    - `list_video_adapters()`
  - 默认已注册 `Veo31Adapter`

### 已完成的接入改造

- `nanobanana.py`
  - 模型/比例/分辨率下拉项改为读取 `model_catalog`

- `veo.py`
  - 模型/比例/分辨率/时长/模式下拉项改为读取 `model_catalog`
  - 历史记录改为 `history_store` 项目级存储
  - MIME 推断改为 `mime_types`
  - `VeoWorker` 请求拼装改为走 `video_model_registry` 适配器

- `gemini_chat.py`
  - Chat 模型列表改为读取 `model_catalog`
  - MIME 策略改为读取 `mime_types`

- `core/api_helpers.py`
  - `get_mime_type` 改为代理 `mime_types.guess_mime_type`

- `core/__init__.py`
  - 已导出新模块接口，方便旧代码渐进迁移

### 接入新视频模型（如 Seedance）

只需新增一个适配器类并注册，不需要改 `VeoWorker` 业务流程。

步骤：
1. 在 `core/video_model_registry.py` 新增 `SeedanceAdapter(BaseVideoAdapter)`
2. 实现 `build_generate_kwargs(...)`
3. 调用 `register_video_adapter(SeedanceAdapter(...))`
4. 在 `core/model_catalog.py` 增加对应 UI 模型项

完成后，UI 选择新模型即可自动走同一套生成管线。

### 依赖方向（避免循环依赖）

- `feature(*.py)` -> `core/*`
- `core/*` 不反向依赖 `feature/*`

这保证了通用层稳定、可测试，业务层可快速扩展。
