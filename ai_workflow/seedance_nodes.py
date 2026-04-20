"""Seedance node creation and manipulation functions.

Follows the same pattern as veo_nodes.py for maintainability.
Contains all functions that create or manipulate Nuke Group/Read nodes for Seedance.
"""

import nuke
import os
import json
import re

from ai_workflow.core.nuke_utils import (
    get_internal_read as _get_internal_read_core,
    next_node_name,
)
from ai_workflow.core.directories import (
    get_input_directory, get_output_directory,
)
from ai_workflow.core.settings import (
    AppSettings as NanoBananaSettings,
)

# ---------------------------------------------------------------------------
# Constants (shared with seedance.py)
# ---------------------------------------------------------------------------
SEEDANCE_MODE_TEXT = "text"
SEEDANCE_MODE_IMAGE = "image"
SEEDANCE_MODE_FRAMES = "frames"
SEEDANCE_MODE_OMNI_REF = "omni_reference"
SEEDANCE_MODE_VIDEO_EXTEND = "video_extend"
SEEDANCE_MODE_AUDIO_DRIVE = "audio_drive"

# Mode -> required node inputs count (for Group Input nodes)
SEEDANCE_MODE_INPUT_COUNTS = {
    SEEDANCE_MODE_TEXT: 0,
    SEEDANCE_MODE_IMAGE: 1,
    SEEDANCE_MODE_FRAMES: 2,
    # omni_reference: uses dynamic reference panel (no fixed inputs)
    SEEDANCE_MODE_OMNI_REF: 0,
    # video_extend: 1 video input
    SEEDANCE_MODE_VIDEO_EXTEND: 1,
    # audio_drive: 1 audio file input
    SEEDANCE_MODE_AUDIO_DRIVE: 1,
}

_SEND_TO_STUDIO_SCRIPT = """
import nuke, socket, json, struct

group = nuke.thisNode()
group.begin()
read_node = nuke.toNode("InternalRead")
group.end()

if not read_node:
    nuke.message("Internal Read node not found.")
else:
    file_path = read_node["file"].value()
    if not file_path:
        nuke.message("No file path set.")
    else:
        data = json.dumps({
            "action": "add_clips",
            "clips": [{
                "file": file_path,
                "name": group.name(),
            }]
        }).encode("utf-8")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", 54321))
            s.sendall(struct.pack(">I", len(data)))
            s.sendall(data)
            resp = s.recv(1024).decode("utf-8")
            s.close()
            nuke.message("Sent to Studio: " + resp)
        except Exception as e:
            nuke.message("Failed to send: " + str(e))
"""


def _get_internal_seedance_read(player_group):
    """Get the internal Read node from a Seedance Player/Viewer Group."""
    return _get_internal_read_core(player_group)


def _add_send_to_studio_knob(read_node):
    """Add a 'Send to Studio' Python button knob to the Read tab of a Read node."""
    if read_node is None:
        return
    try:
        tab = nuke.Tab_Knob("User", "Nuke Studio")
        read_node.addKnob(tab)
        btn = nuke.PyScript_Knob("send_to_studio", "Send to Studio", _SEND_TO_STUDIO_SCRIPT)
        read_node.addKnob(btn)
    except Exception as e:
        print("[Seedance] Warning: could not add Send to Studio knob: {}".format(e))


def _next_seedance_name():
    """Return the next available name like 'seedance1', 'seedance2', etc."""
    used = set()
    for node in nuke.allNodes():
        m = re.match(r"^Seedance(\d+)$", node.name(), re.IGNORECASE)
        if m:
            used.add(int(m.group(1)))
    i = 1
    while i in used:
        i += 1
    return "Seedance{}".format(i)


def _next_seedance_viewer_name():
    """Return the next available name like 'SD_Viewer1', 'SD_Viewer2', etc."""
    used = set()
    for node in nuke.allNodes():
        m = re.match(r"^SD_Viewer(\d+)$", node.name())
        if m:
            used.add(int(m.group(1)))
    i = 1
    while i in used:
        i += 1
    return "SD_Viewer{}".format(i)


def _create_seedance_group_inputs(group_node, input_names):
    """Create Input nodes inside a Seedance Group using NanoBanana/VEO pattern.

    Pattern: REVERSE creation order + 'number' knob.
    """
    count = len(input_names)
    group_node.begin()
    for i in range(count, 0, -1):
        inp = nuke.nodes.Input()
        label = input_names[i - 1]
        inp.setName(label)
        inp["number"].setValue(count - i)
        inp["xpos"].setValue((i - 1) * 200)
        inp["ypos"].setValue(0)
    out = nuke.nodes.Output()
    out["xpos"].setValue(0)
    out["ypos"].setValue(200)
    group_node.end()


def create_seedance_viewer_node(generator_node, prompt, aspect_ratio, duration,
                                output_video_path,
                                reference_image_paths=None,
                                model="doubao-seedance-2-0-260128",
                                resolution="720p",
                                mode="text"):
    """Create a Seedance Viewer node (unified: Read playback + record + regeneration).

    Mirrors VEO Viewer pattern:
      - Tab "Read":       Internal Read node with exposed knobs + Send to Studio
      - Tab "Regenerate": Generation record (read-only) + editable regeneration UI

    Returns:
        (viewer_node, internal_read_node) tuple.
    """
    gen_x = generator_node["xpos"].value()
    gen_y = generator_node["ypos"].value()
    gen_name = generator_node.name()

    # Find existing viewers for THIS generator
    existing_viewers = []
    for node in nuke.allNodes("Group"):
        if "is_seedance_viewer" in node.knobs():
            if "sd_generator" in node.knobs():
                if node["sd_generator"].value() == gen_name:
                    existing_viewers.append(node)

    # Calculate position
    if existing_viewers:
        last_viewer = max(existing_viewers, key=lambda n: n["ypos"].value())
        vx = last_viewer["xpos"].value()
        vy = last_viewer["ypos"].value() + 150
        connect_to = last_viewer
    else:
        vx = gen_x
        vy = gen_y + 150
        connect_to = generator_node

    # Dot node between generator/previous-viewer and this viewer
    dot_node = nuke.nodes.Dot()
    dot_x = int(vx) + 34
    dot_y = int(gen_y + 100) if not existing_viewers else int(vy) - 50
    dot_node["xpos"].setValue(dot_x)
    dot_node["ypos"].setValue(dot_y)
    dot_node.setInput(0, connect_to)

    nuke.Undo.begin("Create Seedance Viewer")
    try:
        group = nuke.nodes.Group()
        group.setName(_next_seedance_viewer_name())
        group["tile_color"].setValue(0xFF6347FF)  # Tomato red (matches toolbar icon)
        group["xpos"].setValue(int(vx))
        group["ypos"].setValue(int(vy))

        # Build internals: Input -> Read -> Output
        group.begin()
        inp_node = nuke.nodes.Input(name="Input")
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # Load video file
        if output_video_path and os.path.exists(output_video_path):
            group.begin()
            read_node["file"].fromUserText(output_video_path)
            if "reload" in read_node.knobs():
                try:
                    read_node["reload"].execute()
                except Exception:
                    pass
            from ai_workflow.core.read_knob_builder import sync_frame_range_from_duration
            sync_frame_range_from_duration(
                read_node, group_node=group, duration=duration,
                prefix="sd_", tag="[Seedance] create_viewer",
            )
            group.end()

        # Connect to Dot
        group.setInput(0, dot_node)

        # Tab 1: Read knobs
        from ai_workflow.core.read_knob_builder import add_read_knobs_to_group
        add_read_knobs_to_group(
            group, read_node,
            prefix="sd_",
            file_value=output_video_path,
            add_frame_range=True,
            add_mov_options=True,
            add_send_to_studio=True,
            send_to_studio_script=_SEND_TO_STUDIO_SCRIPT,
            add_open_read_props=True,
            debug_label="Seedance",
        )

        # Tab 2: Regenerate
        tab_regen = nuke.Tab_Knob("sd_regen_tab", "Regenerate")
        group.addKnob(tab_regen)

        # Hidden knobs storing generation parameters
        for kn_name, kn_cls, default in [
            ("sd_generator", nuke.String_Knob, gen_name),
            ("sd_prompt", nuke.Multiline_Eval_String_Knob, prompt),
            ("sd_ratio", nuke.String_Knob, aspect_ratio),
            ("sd_duration", nuke.String_Knob, str(duration)),
            ("sd_model", nuke.String_Knob, model),
            ("sd_resolution", nuke.String_Knob, resolution),
            ("sd_mode", nuke.String_Knob, mode),
        ]:
            k = kn_cls(kn_name, kn_name.replace("sd_", "").replace("_", " ").title())
            k.setValue(default)
            k.setFlag(nuke.INVISIBLE)
            group.addKnob(k)

        output_knob = nuke.File_Knob("sd_output_path", "Output Video")
        output_knob.setValue((output_video_path or "").replace("\\", "/"))
        output_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(output_knob)

        # Store input reference images as JSON array
        input_img_paths = list(reference_image_paths or [])
        inputs_knob = nuke.Multiline_Eval_String_Knob("sd_input_images", "Input Images (JSON)")
        inputs_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(inputs_knob)
        inputs_knob.setValue(json.dumps(input_img_paths))

        # PyCustom_Knob for regenerate UI
        regen_divider = nuke.Text_Knob("regen_divider", "")
        group.addKnob(regen_divider)
        custom_knob = nuke.PyCustom_Knob(
            "sd_regen_ui", "",
            "ai_workflow.seedance.SeedanceViewerRegenWidget()"
        )
        custom_knob.setFlag(nuke.STARTLINE)
        group.addKnob(custom_knob)

        # Hidden marker
        marker = nuke.Boolean_Knob("is_seedance_viewer", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        # Enable postage-stamp thumbnail
        _update_seedance_thumbnail(group, output_video_path)

        print("Seedance: Created SD_Viewer '{}' with internal Read for: {}".format(
            group.name(), output_video_path))
        return group, read_node
    finally:
        nuke.Undo.end()


def update_seedance_viewer_read(viewer_node, new_video_path, duration=None):
    """Update the internal Read node of a Seedance Viewer with a new video path."""
    if viewer_node is None:
        return None
    internal_read = _get_internal_seedance_read(viewer_node)
    if not internal_read:
        return None

    viewer_node.begin()
    internal_read["file"].fromUserText(new_video_path)
    if "reload" in internal_read.knobs():
        try:
            internal_read["reload"].execute()
        except Exception:
            pass
    from ai_workflow.core.read_knob_builder import sync_frame_range_from_duration
    sync_frame_range_from_duration(
        internal_read, group_node=viewer_node, duration=duration,
        prefix="sd_", tag="[Seedance] update_viewer_read",
    )
    viewer_node.end()

    if "sd_file" in viewer_node.knobs():
        viewer_node["sd_file"].setValue(new_video_path.replace("\\", "/"))
    if "sd_output_path" in viewer_node.knobs():
        viewer_node["sd_output_path"].setValue(new_video_path.replace("\\", "/"))
    return viewer_node


def _update_seedance_thumbnail(node, media_path=None):
    """Enable Nuke postage-stamp thumbnail on a Seedance Viewer Group node."""
    if not node or node.Class() != "Group":
        return
    if media_path and os.path.isfile(media_path):
        try:
            ir = _get_internal_seedance_read(node)
            if ir:
                node.begin()
                ir["file"].fromUserText(media_path)
                node.end()
        except Exception as e:
            print("[Seedance] Thumbnail load error: {}".format(e))

    if "postage_stamp" in node.knobs():
        try:
            node["postage_stamp"].setValue(True)
        except Exception as e:
            print("[Seedance] Thumbnail error: {}".format(e))

    try:
        node.sample("red", 0, 0)
    except Exception:
        pass


def _rebuild_seedance_group_for_thumbnail(node, media_path=None, duration=None):
    """'Replacement Jutsu' - rebuild the Seedance Viewer Group to force thumbnail refresh.

    Same strategy as VEO: copy -> delete -> paste to get a fresh C++ instance.
    """
    if not node or node.Class() != "Group":
        return None
    if "is_seedance_viewer" not in node.knobs():
        return None

    _tag = "[Seedance Rebuild]"

    try:
        node_name = node.name()
        print("{} START for '{}'".format(_tag, node_name))

        nuke.Undo.begin("Seedance Rebuild Thumbnail")

        # Set media_path on InternalRead BEFORE copy
        if media_path and os.path.isfile(media_path):
            ir = _get_internal_seedance_read(node)
            if ir:
                node.begin()
                ir["file"].fromUserText(media_path)
                if "reload" in ir.knobs():
                    try:
                        ir["reload"].execute()
                    except Exception:
                        pass
                from ai_workflow.core.read_knob_builder import sync_frame_range_from_duration
                sync_frame_range_from_duration(
                    ir, group_node=node, duration=duration,
                    prefix="sd_", tag=_tag,
                )
                node.end()
                if "sd_file" in node.knobs():
                    node["sd_file"].setValue(media_path.replace("\\", "/"))
                if "sd_output_path" in node.knobs():
                    node["sd_output_path"].setValue(media_path.replace("\\", "/"))

        # Save connections
        upstream = {}
        for i in range(node.inputs()):
            inp = node.input(i)
            if inp:
                upstream[i] = inp

        downstream = []
        for dep in node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS):
            for i in range(dep.inputs()):
                if dep.input(i) == node:
                    downstream.append((dep, i))

        xpos = int(node["xpos"].value())
        ypos = int(node["ypos"].value())

        # Select & copy
        for n in nuke.allNodes():
            n.setSelected(False)
        node.setSelected(True)
        nuke.nodeCopy("%clipboard%")

        # Delete old
        nuke.delete(node)

        # Paste new
        for n in nuke.allNodes():
            n.setSelected(False)
        nuke.nodePaste("%clipboard%")

        new_node = None
        for n in nuke.selectedNodes():
            if n.Class() == "Group" and "is_seedance_viewer" in n.knobs():
                new_node = n
                break

        if not new_node:
            print("{} ERROR: Could not find pasted node!".format(_tag))
            return None

        # Restore name + position
        if new_node.name() != node_name:
            new_node["name"].setValue(node_name)
        new_node["xpos"].setValue(xpos)
        new_node["ypos"].setValue(ypos)

        # Restore connections
        for idx, up_node in upstream.items():
            try:
                new_node.setInput(idx, up_node)
            except Exception as e:
                print("{} upstream input {} err: {}".format(_tag, idx, e))
        for dep_node, dep_idx in downstream:
            try:
                dep_node.setInput(dep_idx, new_node)
            except Exception as e:
                print("{} downstream '{}' input {} err: {}".format(
                    _tag, dep_node.name(), dep_idx, e))

        if "postage_stamp" in new_node.knobs():
            new_node["postage_stamp"].setValue(True)

        new_node.setSelected(False)
        print("{} DONE — new node '{}' created".format(_tag, new_node.name()))
        nuke.Undo.end()
        return new_node

    except Exception as e:
        import traceback
        print("{} FATAL ERROR: {}\n{}".format(_tag, e, traceback.format_exc()))
        try:
            nuke.Undo.cancel()
        except Exception:
            try:
                nuke.Undo.end()
            except Exception:
                pass
        return None


def create_seedance_viewer_standalone(xpos=None, ypos=None):
    """Manually create an empty Seedance Viewer node (no generator, no video)."""
    nuke.Undo.begin("Create Seedance Viewer")
    try:
        group = nuke.nodes.Group()
        group.setName(_next_seedance_viewer_name())
        group["tile_color"].setValue(0xFF6347FF)

        if xpos is not None:
            group["xpos"].setValue(int(xpos))
        if ypos is not None:
            group["ypos"].setValue(int(ypos))

        group.begin()
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # Tab 1: Read knobs
        from ai_workflow.core.read_knob_builder import add_read_knobs_to_group
        add_read_knobs_to_group(
            group, read_node,
            prefix="sd_",
            file_value="",
            add_frame_range=True,
            add_mov_options=True,
            add_send_to_studio=True,
            send_to_studio_script=_SEND_TO_STUDIO_SCRIPT,
            add_open_read_props=True,
            debug_label="Seedance",
        )

        # Tab 2: Regenerate (empty defaults)
        tab_regen = nuke.Tab_Knob("sd_regen_tab", "Regenerate")
        group.addKnob(tab_regen)

        for kn_name, kn_cls, default in [
            ("sd_generator", nuke.String_Knob, ""),
            ("sd_prompt", nuke.Multiline_Eval_String_Knob, ""),
            ("sd_ratio", nuke.String_Knob, "16:9"),
            ("sd_duration", nuke.String_Knob, "5"),
            ("sd_model", nuke.String_Knob, "doubao-seedance-2-0-260128"),
            ("sd_resolution", nuke.String_Knob, "720p"),
            ("sd_mode", nuke.String_Knob, "text"),
        ]:
            k = kn_cls(kn_name, kn_name.replace("sd_", "").replace("_", " ").title())
            k.setValue(default)
            k.setFlag(nuke.INVISIBLE)
            group.addKnob(k)

        output_knob = nuke.File_Knob("sd_output_path", "Output Video")
        output_knob.setValue("")
        output_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(output_knob)

        inputs_knob = nuke.Multiline_Eval_String_Knob("sd_input_images", "Input Images (JSON)")
        inputs_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(inputs_knob)
        inputs_knob.setValue(json.dumps([]))

        regen_divider = nuke.Text_Knob("regen_divider", "")
        group.addKnob(regen_divider)
        custom_knob = nuke.PyCustom_Knob(
            "sd_regen_ui", "",
            "ai_workflow.seedance.SeedanceViewerRegenWidget()"
        )
        custom_knob.setFlag(nuke.STARTLINE)
        group.addKnob(custom_knob)

        marker = nuke.Boolean_Knob("is_seedance_viewer", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        print("Seedance: Created standalone SD_Viewer '{}'".format(group.name()))
        return group, read_node
    finally:
        nuke.Undo.end()


def _find_seedance_generator(viewer_node):
    """Find the generator name for this Seedance Viewer node."""
    if not viewer_node:
        return ""
    if "sd_generator" in viewer_node.knobs():
        stored = viewer_node["sd_generator"].value() or ""
        if stored:
            return stored

    try:
        visited = set()
        queue = [viewer_node]
        while queue:
            cur = queue.pop(0)
            name = cur.name() if hasattr(cur, 'name') else "?"
            if name in visited:
                continue
            visited.add(name)
            if name.lower().startswith("seedance"):
                if "is_seedance_viewer" not in cur.knobs():
                    return name
            max_inputs = getattr(cur, 'inputs', lambda: 0)()
            for i in range(max_inputs):
                inp = cur.input(i)
                if inp:
                    queue.append(inp)
    except Exception as e:
        print("[Seedance] Error walking upstream: {}".format(e))
    return ""


def _collect_seedance_input_image_paths(node):
    """Collect reference images for a Seedance Viewer node.

    Priority 1: sd_input_images JSON knob.
    Priority 2: Scan input cache dir by generator name.
    """
    if node and "sd_input_images" in node.knobs():
        try:
            raw = node["sd_input_images"].value()
            if raw and raw.strip():
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    paths = [p for p in parsed if p]
                    if paths:
                        return paths
        except Exception as e:
            print("[Seedance] JSON knob parse error: {}".format(e))

    gen_name = _find_seedance_generator(node)
    if gen_name:
        paths = []
        try:
            input_dir = get_input_directory()
            if os.path.isdir(input_dir):
                prefix = "{}_".format(gen_name)
                extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                for fname in sorted(os.listdir(input_dir)):
                    if fname.startswith(prefix):
                        ext = os.path.splitext(fname)[1].lower()
                        if ext in extensions:
                            paths.append(os.path.join(input_dir, fname).replace("\\", "/"))
                if paths:
                    return paths
        except Exception as e:
            print("[Seedance] Error scanning input dir: {}".format(e))
    return []


def create_seedance_node():
    """
    Create a Seedance node.
    Auto-detect mode based on selected node count:
      - 0 nodes selected -> Text mode (0 inputs, default)
      - 1 node selected  -> Image mode (1 input: first frame)
      - 2 nodes selected -> Frames mode (2 inputs: first + last frame)
    """
    sel = list(reversed(nuke.selectedNodes()))
    node_count = len(sel)

    if node_count == 0:
        auto_mode = SEEDANCE_MODE_TEXT
    elif node_count == 1:
        auto_mode = SEEDANCE_MODE_IMAGE
    elif node_count >= 2:
        auto_mode = SEEDANCE_MODE_FRAMES
        sel = sel[:2]
    else:
        auto_mode = SEEDANCE_MODE_TEXT

    needed_inputs = SEEDANCE_MODE_INPUT_COUNTS.get(auto_mode, 0)
    input_names = {
        SEEDANCE_MODE_TEXT: [],
        SEEDANCE_MODE_IMAGE: ["FirstFrame"],
        SEEDANCE_MODE_FRAMES: ["FirstFrame", "EndFrame"],
        SEEDANCE_MODE_VIDEO_EXTEND: ["VideoIn"],
        SEEDANCE_MODE_AUDIO_DRIVE: ["AudioIn"],
    }.get(auto_mode, [])

    ref_node = sel[0] if sel else None

    group_node = nuke.nodes.Group()
    group_node.setName(_next_seedance_name())
    group_node["tile_color"].setValue(0xFF6347FF)  # Tomato red

    if ref_node:
        sx = int(ref_node["xpos"].value())
        sy = int(ref_node["ypos"].value())
        group_node["xpos"].setValue(sx)
        group_node["ypos"].setValue(sy + 100)
    else:
        try:
            center = nuke.center()
            x, y = int(center[0]), int(center[1])
        except Exception:
            x, y = 0, 0
        group_node["xpos"].setValue(x)
        group_node["ypos"].setValue(y)

    _create_seedance_group_inputs(group_node, input_names[:needed_inputs])

    # Connect selected nodes
    for k, src_node in enumerate(sel[:needed_inputs]):
        port_idx = needed_inputs - 1 - k
        group_node.setInput(port_idx, src_node)

    # Add custom Seedance tab with PyCustom_Knob
    tab = nuke.Tab_Knob("seedance_tab", "Seedance")
    group_node.addKnob(tab)

    custom_knob = nuke.PyCustom_Knob(
        "seedance_ui", "",
        "ai_workflow.seedance.SeedanceKnobWidget()"
    )
    custom_knob.setFlag(nuke.STARTLINE)
    group_node.addKnob(custom_knob)

    # Pre-save mode state
    _MODE_TO_INDEX = {
        SEEDANCE_MODE_TEXT: 0,
        SEEDANCE_MODE_IMAGE: 1,
        SEEDANCE_MODE_FRAMES: 2,
        SEEDANCE_MODE_OMNI_REF: 3,
        SEEDANCE_MODE_VIDEO_EXTEND: 4,
        SEEDANCE_MODE_AUDIO_DRIVE: 5,
    }
    mode_idx = _MODE_TO_INDEX.get(auto_mode, 0)
    mode_knob = nuke.Int_Knob("sd_s_mode", "s_mode")
    mode_knob.setVisible(False)
    group_node.addKnob(mode_knob)
    group_node["sd_s_mode"].setValue(mode_idx)

    mode_display = {
        SEEDANCE_MODE_TEXT: "Text",
        SEEDANCE_MODE_IMAGE: "Image(first frame)",
        SEEDANCE_MODE_FRAMES: "Frames(first+last)",
        SEEDANCE_MODE_OMNI_REF: "OmniReference(multi-ref)",
        SEEDANCE_MODE_VIDEO_EXTEND: "VideoExtend",
        SEEDANCE_MODE_AUDIO_DRIVE: "AudioDrive",
    }
    print("Seedance: Created '{}' with auto-detected mode: {} (selected {} nodes)".format(
        group_node.name(), mode_display.get(auto_mode, auto_mode), node_count))

    return group_node
