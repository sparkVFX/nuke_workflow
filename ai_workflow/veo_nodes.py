"""VEO node creation and manipulation functions.

Extracted from veo.py for maintainability. Contains all
functions that create or manipulate Nuke Group/Read/Dot nodes.

Backward-compatible re-exports are added to veo.py so that
existing code continues to work.
"""

import nuke
import os
import json
import re
import time
import datetime

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
# Constants (shared with veo.py — defined here to avoid circular imports)
# ---------------------------------------------------------------------------
VEO_MODELS = {
    "Google VEO 3.1": "veo-3.1-generate-preview",
    "Google VEO 3.1-Fast": "veo-3.1-fast-generate-preview",
}
VEO_MODEL_DEFAULT = "Google VEO 3.1-Fast"

VEO_MODE_TEXT = "Text"
VEO_MODE_FIRST_FRAME = "FirstFrame"
VEO_MODE_FRAMES = "Frames"
VEO_MODE_INGREDIENTS = "Ingredients"

VEO_MODE_INPUT_COUNTS = {
    VEO_MODE_TEXT: 0,
    VEO_MODE_FIRST_FRAME: 1,
    VEO_MODE_FRAMES: 2,
    VEO_MODE_INGREDIENTS: 3,
}

_SEND_TO_STUDIO_SCRIPT = """
import nuke, socket, json, struct

node = nuke.thisNode()
file_path = node["file"].value()
if not file_path:
    nuke.message("No file path set on this Read node.")
else:
    data = json.dumps({
        "action": "add_clips",
        "clips": [{
            "file": file_path,
            "name": node.name(),
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
        print("Warning: could not add Send to Studio knob: {}".format(e))
_VEO_PLAYER_SEND_SCRIPT = """
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
def create_veo_player_node(video_path=None, name=None, xpos=None, ypos=None):
    """Create a VEO Player Group node that wraps a Read node.

    The Group exposes all Read-tab knobs using REAL knobs (NOT Link_Knob)
    so they survive rename-undo (Ctrl+Z).  A knobChanged callback keeps
    them synced with the internal Read node via name-based lookup
    (nuke.toNode('InternalRead')).

    Args:
        video_path: Optional path to a video file to load.
        name: Optional node name.
        xpos, ypos: Optional position.

    Returns:
        (group_node, internal_read_node) tuple.
    """
    # Wrap entire node creation as a single undo unit so that
    # subsequent rename-undo cannot partially break internal structure
    nuke.Undo.begin("Create VEO Viewer")
    try:
        # --- Create the Group shell ---
        _default_name = _next_veo_name()
        group = nuke.nodes.Group(name=(name or _default_name))
        group["tile_color"].setValue(0x00C878FF)  # Green tint
        group["label"].setValue("VEO Player")

        if xpos is not None:
            group["xpos"].setValue(int(xpos))
        if ypos is not None:
            group["ypos"].setValue(int(ypos))

        # --- Build internals: Read → Output ---
        group.begin()
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # Load the video file AFTER group.end() so knobs are fully populated
        if video_path and os.path.exists(video_path):
            group.begin()
            read_node["file"].fromUserText(video_path)
            group.end()

        # --- Expose Read-tab knobs on the Group panel ---
        # Use REAL knobs (NOT Link_Knob) so they survive rename-undo.
        from ai_workflow.core.read_knob_builder import add_read_knobs_to_group
        _read_sync_knobs = add_read_knobs_to_group(
            group, read_node,
            prefix="veo_",
            file_value=video_path,
            add_frame_range=True,
            add_mov_options=True,
            add_send_to_studio=True,
            send_to_studio_script=_VEO_PLAYER_SEND_SCRIPT,
            add_open_read_props=True,
            debug_label="VEO",
            extra_debug_in_not_found=True,
        )

        # Hidden marker knob so we can identify VEO Player nodes later
        marker = nuke.Boolean_Knob("is_veo_player", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        print("VEO: Created VEO Player '{}' with internal Read".format(group.name()))
        # DEBUG: dump all knobs on the group
        print("[VEO-DEBUG] Group knobs after creation:")
        for _gk in group.knobs():
            try:
                _gv = str(group[_gk].value())[:60]
            except Exception:
                _gv = "(no value)"
            print("  [VEO-DEBUG]   %s = %s (type=%s)" % (_gk, _gv, group[_gk].Class()))
        # DEBUG: verify InternalRead is accessible by name
        group.begin()
        _dr = nuke.toNode('InternalRead')
        group.end()
        print("[VEO-DEBUG] InternalRead accessible immediately after creation: %s" % (_dr is not None))
        if _dr:
            for _rk in ['file', 'format', 'first', 'last', 'colorspace']:
                if _rk in _dr.knobs():
                    try:
                        print("  [VEO-DEBUG]   Read.%s = %s" % (_rk, str(_dr[_rk].value())[:60]))
                    except Exception:
                        pass

        return group, read_node

    finally:
        nuke.Undo.end()


# Use core's get_internal_read for VEO nodes too
def _get_internal_read(player_group):
    """Get the internal Read node from a VEO Player/Viewer Group."""
    return _get_internal_read_core(player_group)
def _rebuild_veo_group_for_thumbnail(node, media_path=None, duration=None):
    """'Replacement Jutsu' — rebuild the VEO Viewer Group node to force thumbnail refresh.

    Nuke's Group-node postage-stamp cache is bound to the C++ node instance
    and cannot be flushed via any public Python / Tcl API.  The only reliable
    way to make the DAG show a new thumbnail is to **replace the node with
    an identical copy** (same strategy proven in NanoBanana).

    Strategy:
      1. Save all upstream / downstream connections
      2. nuke.nodeCopy  -> clipboard  (serialises the Group + its internals)
      3. Delete old node
      4. nuke.nodePaste -> new node   (fresh C++ instance -> fresh thumbnail)
      5. Restore all connections & ensure the new node keeps the same name

    If *media_path* is given the InternalRead is pointed to that file
    **before** the copy so the pasted clone already has the right media.

    If *duration* is given (seconds, str or number) it's used as fallback to
    compute frame range (duration × fps) when origfirst/origlast = 1/1.

    Returns the **new** Group node (the old reference is now invalid).
    Returns *None* on failure (caller should fall back to legacy approach).
    """
    if not node or node.Class() != "Group":
        return None
    if "is_veo_viewer" not in node.knobs():
        return None  # safety — only operate on VEO Viewer nodes

    _tag = "[VEO Rebuild]"

    try:
        node_name = node.name()
        print("{} START for '{}'".format(_tag, node_name))

        # Wrap in Undo group so the whole operation can be reverted if needed
        nuke.Undo.begin("VEO Rebuild Thumbnail")

        # --- 0. Set media_path on InternalRead BEFORE copy ---
        if media_path and os.path.isfile(media_path):
            ir = _get_internal_read(node)
            if ir:
                node.begin()
                ir["file"].fromUserText(media_path)
                # Force reload so the Read parses the new video file header
                if "reload" in ir.knobs():
                    try:
                        ir["reload"].execute()
                    except Exception:
                        pass
                # Sync first/last to origfirst/origlast so the Read plays full range
                from ai_workflow.core.read_knob_builder import sync_frame_range_from_duration
                _range_set = sync_frame_range_from_duration(
                    ir, group_node=node, duration=duration,
                    prefix="veo_", tag=_tag,
                )
                node.end()
                # Also sync Group-level veo_file knob
                if "veo_file" in node.knobs():
                    node["veo_file"].setValue(media_path.replace("\\", "/"))
                if "veo_output_path" in node.knobs():
                    node["veo_output_path"].setValue(media_path.replace("\\", "/"))

        # --- 1. Save connections ---
        # Upstream (inputs to this node)
        upstream = {}  # {input_index: upstream_node}
        for i in range(node.inputs()):
            inp = node.input(i)
            if inp:
                upstream[i] = inp

        # Downstream (nodes that take this node as input)
        downstream = []  # [(dependent_node, input_index)]
        for dep in node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS):
            for i in range(dep.inputs()):
                if dep.input(i) == node:
                    downstream.append((dep, i))

        # Save position
        xpos = int(node["xpos"].value())
        ypos = int(node["ypos"].value())

        # --- 2. Select only this node and copy to clipboard ---
        for n in nuke.allNodes():
            n.setSelected(False)
        node.setSelected(True)

        nuke.nodeCopy("%clipboard%")
        print("{}   nodeCopy OK".format(_tag))

        # --- 3. Delete old node ---
        nuke.delete(node)
        print("{}   deleted old '{}'".format(_tag, node_name))

        # --- 4. Paste from clipboard ---
        for n in nuke.allNodes():
            n.setSelected(False)

        nuke.nodePaste("%clipboard%")
        print("{}   nodePaste OK".format(_tag))

        # The pasted node(s) are selected — find our new Group
        new_node = None
        for n in nuke.selectedNodes():
            if n.Class() == "Group" and "is_veo_viewer" in n.knobs():
                new_node = n
                break

        if not new_node:
            print("{} ERROR: Could not find pasted node!".format(_tag))
            return None

        # --- 5. Restore name ---
        if new_node.name() != node_name:
            new_node["name"].setValue(node_name)
            print("{}   renamed to '{}'".format(_tag, node_name))

        # Restore position (nodePaste offsets by +10,+10)
        new_node["xpos"].setValue(xpos)
        new_node["ypos"].setValue(ypos)

        # --- 6. Restore connections ---
        for idx, up_node in upstream.items():
            try:
                new_node.setInput(idx, up_node)
            except Exception as e:
                print("{}   upstream input {} err: {}".format(_tag, idx, e))

        for dep_node, dep_idx in downstream:
            try:
                dep_node.setInput(dep_idx, new_node)
            except Exception as e:
                print("{}   downstream '{}' input {} err: {}".format(
                    _tag, dep_node.name(), dep_idx, e))

        # --- 7. Ensure postage_stamp is ON ---
        if "postage_stamp" in new_node.knobs():
            new_node["postage_stamp"].setValue(True)

        # --- 8. Verify internal Read frame range after paste ---
        # nodeCopy/nodePaste serialises knob values, so the range set in
        # step 0 should survive.  But as a safety net, re-check and fix.
        try:
            new_ir = _get_internal_read(new_node)
            if new_ir:
                new_node.begin()
                from ai_workflow.core.read_knob_builder import sync_frame_range_from_duration
                sync_frame_range_from_duration(
                    new_ir, group_node=new_node, duration=duration,
                    prefix="veo_", tag=_tag,
                    push_group_to_read=True,
                )
                new_node.end()
        except Exception as _vre:
            print("{}   frame range verify error: {}".format(_tag, _vre))

        # Deselect
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


def _update_veo_thumbnail(node, media_path=None):
    """Enable Nuke postage-stamp thumbnail on a VEO Viewer Group node.

    Mirrors NanoBanana's ``_update_node_thumbnail`` but adapted for
    video (VEO) content.  The key steps are:
      1. Ensure InternalRead has the correct file loaded.
      2. Enable the ``postage_stamp`` knob.
      3. Force pixel computation so there's data to render.
      4. Trigger DAG refresh so the thumbnail appears immediately.

    NOTE: This is the *fallback* method.  For reliable thumbnail refresh,
    use ``_rebuild_veo_group_for_thumbnail`` (Replacement Jutsu) first.
    """
    if not node or node.Class() != "Group":
        print("[VEO] Thumbnail: skip — invalid node")
        return

    # 1. Ensure InternalRead points to the media file
    if media_path and os.path.isfile(media_path):
        try:
            ir = _get_internal_read(node)
            if ir:
                node.begin()
                ir["file"].fromUserText(media_path)
                node.end()
                print("[VEO] Thumbnail: InternalRead loaded {}".format(media_path))
            else:
                print("[VEO] Thumbnail: WARNING no InternalRead found")
        except Exception as e:
            print("[VEO] Thumbnail: load error: {}".format(e))

    # 2. Enable postage_stamp
    if "postage_stamp" in node.knobs():
        try:
            node["postage_stamp"].setValue(True)
            print("[VEO] Thumbnail: postage_stamp enabled on '{}'".format(
                node.name()))
        except Exception as e:
            print("[VEO] Thumbnail: postage_stamp error: {}".format(e))
    else:
        print("[VEO] Thumbnail: WARNING no postage_stamp knob")

    # 3. Force pixel computation
    try:
        node.sample("red", 0, 0)
    except Exception:
        pass

    # 4. Force DAG refresh — toggle postage_stamp off/on + modified()
    try:
        node["postage_stamp"].setValue(False)
        node["postage_stamp"].setValue(True)
        nuke.modified()
    except Exception:
        pass

def _next_veo_viewer_name():
    """Return the next available name like 'VEO_Viewer1', 'VEO_Viewer2', etc."""
    used = set()
    for node in nuke.allNodes():
        m = re.match(r"^VEO_Viewer(\d+)$", node.name())
        if m:
            used.add(int(m.group(1)))
    i = 1
    while i in used:
        i += 1
    return "VEO_Viewer{}".format(i)
def create_veo_viewer_node(generator_node, prompt, aspect_ratio, duration,
                           output_video_path,
                           reference_image_paths=None,
                           model="Google VEO 3.1-Fast",
                           resolution="720P",
                           mode="Text",
                           negative_prompt=""):
    """Create a unified VEO Viewer node (like NanoBanana's Nano Viewer).

    Combines the old sp (record) + VEO Player (Read) into a single Group:
      - Tab "Read":       Internal Read node with exposed knobs + Send to Studio
      - Tab "Regenerate": Generation record (read-only) + editable regeneration UI

    Node chain:  Generator → Dot → VEO_Viewer  (no separate sp / Player nodes)

    Returns:
        (viewer_node, internal_read_node)
    """
    gen_x = generator_node["xpos"].value()
    gen_y = generator_node["ypos"].value()
    gen_name = generator_node.name()

    # Find existing VEO Viewer nodes for THIS generator
    existing_viewers = []
    for node in nuke.allNodes("Group"):
        if "is_veo_viewer" in node.knobs():
            if "veo_generator" in node.knobs():
                if node["veo_generator"].value() == gen_name:
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

    # --- Dot node between generator/previous-viewer and this viewer ---
    dot_node = nuke.nodes.Dot()
    dot_x = int(vx) + 34
    dot_y = int(connect_to["ypos"].value()) + 80 if not existing_viewers else int(vy) - 50
    if not existing_viewers:
        dot_y = int(gen_y) + 100
    dot_node["xpos"].setValue(dot_x)
    dot_node["ypos"].setValue(dot_y)
    dot_node.setInput(0, connect_to)

    viewer_num = len(existing_viewers) + 1

    # ================================================================
    # Create the unified Group node
    # ================================================================
    nuke.Undo.begin("Create VEO Viewer")
    try:
        group = nuke.nodes.Group()
        group.setName(_next_veo_viewer_name())
        group["tile_color"].setValue(0x2E2E2EFF)  # Dark grey (same as Nano Viewer)
        group["xpos"].setValue(int(vx))
        group["ypos"].setValue(int(vy))

        # --- Build internals: Input → Read → Output ---
        group.begin()
        inp_node = nuke.nodes.Input(name="Input")
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # Load the video file AFTER group.end() so knobs are fully populated
        if output_video_path and os.path.exists(output_video_path):
            group.begin()
            read_node["file"].fromUserText(output_video_path)
            # Force Read to re-parse the video so metadata (frame range etc.) is available
            if "reload" in read_node.knobs():
                try:
                    read_node["reload"].execute()
                except Exception:
                    pass
            # Explicitly set first/last = origfirst/origlast so video plays full range
            from ai_workflow.core.read_knob_builder import sync_frame_range_from_duration
            sync_frame_range_from_duration(
                read_node, group_node=group, duration=duration,
                prefix="veo_", tag="[VEO] create_veo_viewer_node",
            )
            group.end()

        # Connect to the Dot
        group.setInput(0, dot_node)

        # ==============================================================
        # Tab 1: Read  (REAL knobs — NOT Link_Knob — survive rename-undo)
        # ==============================================================
        from ai_workflow.core.read_knob_builder import add_read_knobs_to_group
        _read_sync_knobs = add_read_knobs_to_group(
            group, read_node,
            prefix="veo_",
            file_value=output_video_path,
            add_frame_range=True,
            add_mov_options=True,
            add_send_to_studio=True,
            send_to_studio_script=_VEO_PLAYER_SEND_SCRIPT,
            add_open_read_props=True,
            debug_label="VEO",
            extra_debug_in_not_found=False,
        )

        # Tab 2: Regenerate  (generation record + editable regeneration UI)
        # ==============================================================
        tab_regen = nuke.Tab_Knob("veo_regen_tab", "Regenerate")
        group.addKnob(tab_regen)

        # --- Hidden knobs storing generation parameters ---
        gen_knob = nuke.String_Knob("veo_generator", "Generator")
        gen_knob.setValue(gen_name)
        gen_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(gen_knob)

        prompt_knob = nuke.Multiline_Eval_String_Knob("veo_prompt", "Prompt")
        prompt_knob.setValue(prompt)
        prompt_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(prompt_knob)

        ratio_knob = nuke.String_Knob("veo_ratio", "Aspect Ratio")
        ratio_knob.setValue(aspect_ratio)
        ratio_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(ratio_knob)

        dur_knob = nuke.String_Knob("veo_duration", "Duration")
        dur_knob.setValue(duration if duration else "8")
        dur_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(dur_knob)

        model_knob = nuke.String_Knob("veo_model", "Model")
        model_knob.setValue(model)
        model_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(model_knob)

        res_knob = nuke.String_Knob("veo_resolution", "Resolution")
        res_knob.setValue(resolution)
        res_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(res_knob)

        mode_knob = nuke.String_Knob("veo_mode", "Mode")
        mode_knob.setValue(mode)
        mode_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(mode_knob)

        neg_prompt_knob = nuke.Multiline_Eval_String_Knob("veo_neg_prompt", "Negative Prompt")
        neg_prompt_knob.setValue(negative_prompt)
        neg_prompt_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(neg_prompt_knob)

        output_knob = nuke.File_Knob("veo_output_path", "Output Video")
        output_knob.setValue(output_video_path.replace("\\", "/") if output_video_path else "")
        output_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(output_knob)

        # Store input reference images as JSON array (same format as NanoBanana)
        input_img_paths = list(reference_image_paths or [])
        print("[VEO Viewer] veo_input_images: storing {} paths".format(len(input_img_paths)))
        for _ip in input_img_paths:
            print("  [VEO Viewer]   -> {}".format(_ip))
        inputs_knob = nuke.Multiline_Eval_String_Knob("veo_input_images", "Input Images (JSON)")
        inputs_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(inputs_knob)
        inputs_knob.setValue(json.dumps(input_img_paths))

        # --- PyCustom_Knob for regenerate UI ---
        regen_divider = nuke.Text_Knob("regen_divider", "")
        group.addKnob(regen_divider)

        custom_knob = nuke.PyCustom_Knob(
            "veo_regen_ui", "",
            "ai_workflow.veo.VeoViewerRegenWidget()"
        )
        custom_knob.setFlag(nuke.STARTLINE)
        group.addKnob(custom_knob)

        # --- Hidden marker knob to identify VEO Viewer nodes ---
        marker = nuke.Boolean_Knob("is_veo_viewer", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        # --- Enable postage-stamp thumbnail (like NanoBanana) ---
        _update_veo_thumbnail(group, output_video_path)

        # --- Final sync: push Group-level frame range INTO internal Read ---
        # At creation time, knobChanged doesn't fire for initial values,
        # so the internal Read may still have first/last=1 even though the
        # Group-level veo_first/veo_last are correct.  Push them explicitly.
        try:
            group.begin()
            for _gk, _rk in [("veo_first", "first"), ("veo_last", "last"),
                              ("veo_origfirst", "origfirst"), ("veo_origlast", "origlast")]:
                if _gk in group.knobs() and _rk in read_node.knobs():
                    _gv = int(group[_gk].value())
                    _rv = int(read_node[_rk].value())
                    if _gv != _rv:
                        read_node[_rk].setValue(_gv)
                        print("[VEO] create_veo_viewer_node: synced {} -> InternalRead.{} = {}".format(
                            _gk, _rk, _gv))
            group.end()
        except Exception as _sync_e:
            print("[VEO] create_veo_viewer_node: final sync error: {}".format(_sync_e))
            try:
                group.end()
            except Exception:
                pass

        print("VEO: Created VEO Viewer '{}' with internal Read for: {}".format(
            group.name(), output_video_path))
        return group, read_node
    finally:
        nuke.Undo.end()


def create_veo_viewer_standalone(xpos=None, ypos=None):
    """Manually create an empty VEO Viewer node (no generator, no video).

    Called from the toolbar menu for standalone testing / manual usage.
    Mirrors NanoBanana's ``create_nb_player_node(xpos, ypos)`` pattern.

    Returns:
        (viewer_group, internal_read_node)
    """
    nuke.Undo.begin("Create VEO Viewer")
    try:
        group = nuke.nodes.Group()
        group.setName(_next_veo_viewer_name())
        group["tile_color"].setValue(0x2E2E2EFF)  # Dark grey (same as Nano Viewer)

        if xpos is not None:
            group["xpos"].setValue(int(xpos))
        if ypos is not None:
            group["ypos"].setValue(int(ypos))

        # --- Build internals: Read → Output ---
        group.begin()
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()
        # ==============================================================
        # Tab 1: Read  (REAL knobs — NOT Link_Knob — survive rename-undo)
        # ==============================================================
        from ai_workflow.core.read_knob_builder import add_read_knobs_to_group
        _read_sync_knobs = add_read_knobs_to_group(
            group, read_node,
            prefix="veo_",
            file_value="",
            add_frame_range=True,
            add_mov_options=True,
            add_send_to_studio=True,
            send_to_studio_script=_VEO_PLAYER_SEND_SCRIPT,
            add_open_read_props=True,
            debug_label="VEO",
            extra_debug_in_not_found=False,
        )


        # ==============================================================
        # Tab 2: Regenerate  (empty – no generation parameters for standalone)
        # ==============================================================
        tab_regen = nuke.Tab_Knob("veo_regen_tab", "Regenerate")
        group.addKnob(tab_regen)

        # Hidden knobs with empty defaults (so the widget and other code doesn't crash)
        for kn_name, kn_cls, default in [
            ("veo_generator", nuke.String_Knob, ""),
            ("veo_prompt", nuke.Multiline_Eval_String_Knob, ""),
            ("veo_ratio", nuke.String_Knob, "16:9"),
            ("veo_duration", nuke.String_Knob, "8"),
            ("veo_model", nuke.String_Knob, ""),
            ("veo_resolution", nuke.String_Knob, "720P"),
            ("veo_mode", nuke.String_Knob, "Text"),
            ("veo_neg_prompt", nuke.Multiline_Eval_String_Knob, ""),
        ]:
            k = kn_cls(kn_name, kn_name.replace("veo_", "").replace("_", " ").title())
            k.setValue(default)
            k.setFlag(nuke.INVISIBLE)
            group.addKnob(k)

        output_knob = nuke.File_Knob("veo_output_path", "Output Video")
        output_knob.setValue("")
        output_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(output_knob)

        inputs_knob = nuke.Multiline_Eval_String_Knob("veo_input_images", "Input Images (JSON)")
        inputs_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(inputs_knob)
        inputs_knob.setValue(json.dumps([]))

        # --- PyCustom_Knob for regenerate UI ---
        regen_divider = nuke.Text_Knob("regen_divider", "")
        group.addKnob(regen_divider)

        custom_knob = nuke.PyCustom_Knob(
            "veo_regen_ui", "",
            "ai_workflow.veo.VeoViewerRegenWidget()"
        )
        custom_knob.setFlag(nuke.STARTLINE)
        group.addKnob(custom_knob)

        # --- Hidden marker ---
        marker = nuke.Boolean_Knob("is_veo_viewer", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        print("VEO: Created standalone VEO Viewer '{}'".format(group.name()))
        return group, read_node
    finally:
        nuke.Undo.end()


def update_veo_viewer_read(viewer_node, new_video_path, duration=None):
    """Update the internal Read node of a VEO Viewer with a new video path.
    If the viewer node doesn't exist or is invalid, create a new VEO Viewer.

    Args:
        duration: Video duration in seconds (str or number). Used as fallback
                  to compute frame range (duration × fps) when the Read node's
                  origfirst/origlast can't be parsed from the MOV header.
    """
    if viewer_node is not None:
        internal_read = _get_internal_read(viewer_node)
        if internal_read:
            viewer_node.begin()
            internal_read["file"].fromUserText(new_video_path)
            # Force Read to re-parse video metadata (format, frame range, etc.)
            # Without this, first/last/origfirst/origlast may stay at default=1
            # because Nuke hasn't finished parsing the new MOV file yet.
            if "reload" in internal_read.knobs():
                try:
                    internal_read["reload"].execute()
                    print("[VEO] update_veo_viewer_read: reload executed for '{}'".format(
                        new_video_path))
                except Exception as _re:
                    print("[VEO] update_veo_viewer_read: reload failed: {}".format(_re))
            # After reload, Nuke updates origfirst/origlast from the file header,
            # but first/last (the user-editable frame range) may NOT auto-sync.
            # We must explicitly set first/last = origfirst/origlast so the Read
            # node actually plays the full frame range of the video.
            from ai_workflow.core.read_knob_builder import sync_frame_range_from_duration
            sync_frame_range_from_duration(
                internal_read, group_node=viewer_node, duration=duration,
                prefix="veo_", tag="[VEO] update_veo_viewer_read",
            )
            viewer_node.end()
            if "veo_file" in viewer_node.knobs():
                viewer_node["veo_file"].setValue(new_video_path.replace("\\", "/"))
            # Sync format from Read to Group
            try:
                _fv = internal_read["format"].value()
                _fmt_name = ""
                if hasattr(_fv, "name") and _fv.name():
                    _fmt_name = _fv.name()
                elif hasattr(_fv, "width") and _fv.width() > 0:
                    _fmt_name = "%dx%d" % (_fv.width(), _fv.height())
                if _fmt_name and "veo_format" in viewer_node.knobs():
                    _cur_vals = list(viewer_node["veo_format"].values())
                    if _fmt_name not in _cur_vals:
                        _cur_vals.append(_fmt_name)
                        viewer_node["veo_format"].setValues(_cur_vals)
                    viewer_node["veo_format"].setValue(_fmt_name)
            except Exception:
                pass
            # Sync frame range from Read to Group
            for _rk, _gk in [("first", "veo_first"), ("last", "veo_last"),
                              ("origfirst", "veo_origfirst"), ("origlast", "veo_origlast")]:
                try:
                    if _rk in internal_read.knobs() and _gk in viewer_node.knobs():
                        viewer_node[_gk].setValue(int(internal_read[_rk].value()))
                except Exception:
                    pass
            # Fallback: if Read first/last are still 1/1 but Group has correct values,
            # push Group -> Read (the Group may already have correct range from creation)
            try:
                _rf = int(internal_read["first"].value()) if "first" in internal_read.knobs() else 1
                _rl = int(internal_read["last"].value()) if "last" in internal_read.knobs() else 1
                if _rl <= _rf:
                    # Read range is bad — try to get correct values from Group knobs
                    _gf = int(viewer_node["veo_first"].value()) if "veo_first" in viewer_node.knobs() else 1
                    _gl = int(viewer_node["veo_last"].value()) if "veo_last" in viewer_node.knobs() else 1
                    if _gl > _gf:
                        viewer_node.begin()
                        internal_read["first"].setValue(_gf)
                        internal_read["last"].setValue(_gl)
                        if "origfirst" in internal_read.knobs():
                            internal_read["origfirst"].setValue(_gf)
                        if "origlast" in internal_read.knobs():
                            internal_read["origlast"].setValue(_gl)
                        viewer_node.end()
                        print("[VEO] update_veo_viewer_read: pushed Group range {}-{} -> Read".format(_gf, _gl))
            except Exception as _fb_e:
                print("[VEO] update_veo_viewer_read: fallback push error: {}".format(_fb_e))
            # Sync colorspace from Read to Group
            try:
                if "colorspace" in internal_read.knobs() and "veo_colorspace" in viewer_node.knobs():
                    _cv = str(internal_read["colorspace"].value())
                    viewer_node["veo_colorspace"].setValue(_cv)
            except Exception:
                pass
        if "veo_output_path" in viewer_node.knobs():
            viewer_node["veo_output_path"].setValue(new_video_path.replace("\\", "/"))
        return viewer_node
    return None
def _find_veo_generator(viewer_node):
    """Find the generator name for this VEO Viewer node.

    Priority 1: veo_generator knob (fast path).
    Priority 2: Walk upstream to find a VEO_Generate node.
    """
    if not viewer_node:
        return ""

    # Fast path: stored generator name
    if "veo_generator" in viewer_node.knobs():
        stored = viewer_node["veo_generator"].value() or ""
        if stored:
            return stored

    # Slow path: walk upstream
    try:
        visited = set()
        queue = [viewer_node]
        while queue:
            cur = queue.pop(0)
            name = cur.name() if hasattr(cur, "name") else "?"
            if name in visited:
                continue
            visited.add(name)
            if name.startswith("veo") or name.startswith("VEO"):
                if "is_veo_viewer" not in cur.knobs():
                    return name
            max_inputs = getattr(cur, "inputs", lambda: 0)()
            for i in range(max_inputs):
                inp = cur.input(i)
                if inp:
                    queue.append(inp)
    except Exception as e:
        print("[VEO InputScan] Error walking upstream: {}".format(e))

    return ""


def _collect_veo_input_images_for_round(gen_name):
    """Find cached input images for a specific VEO generation round.

    Scans get_input_directory() for files matching:
      {GenName}_{Label}_frame{N}.png
    """
    paths = []
    try:
        input_dir = get_input_directory()
        if not os.path.isdir(input_dir):
            return paths

        prefix = "{}_".format(gen_name)
        extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        for fname in sorted(os.listdir(input_dir)):
            if fname.startswith(prefix):
                ext = os.path.splitext(fname)[1].lower()
                if ext in extensions:
                    fpath = os.path.join(input_dir, fname).replace("\\", "/")
                    paths.append(fpath)

        print("[VEO InputScan] Found {} image(s) for gen='{}' in input dir".format(
            len(paths), gen_name))
        for p in paths:
            exists = os.path.exists(p)
            print("  [VEO InputScan]   -> {} [{}]".format(p, "OK" if exists else "MISSING"))
    except Exception as e:
        print("[VEO InputScan] Error scanning input dir for '{}': {}".format(gen_name, e))

    return paths


def _collect_veo_input_image_paths(node):
    """Main entry point: collect reference images for this VEO Viewer node.

    Priority 1 (primary): Read veo_input_images JSON knob.
    Priority 2 (fallback): Scan input cache dir via generator name.

    Returns list of file path strings (same format as NanoBanana).
    """
    # Step 1: PRIMARY — try JSON knob first
    if node and "veo_input_images" in node.knobs():
        try:
            raw = node["veo_input_images"].value()
            print("[VEO InputScan] Step 1: knob exists, raw length={} chars".format(
                len(raw) if raw else 0))
            if raw and raw.strip():
                parsed = json.loads(raw)
                # Support both formats:
                # New: plain array ["path1", "path2"]
                # Old: {"reference_images": ["path1", "path2"]}
                if isinstance(parsed, list):
                    paths = [p for p in parsed if p]
                elif isinstance(parsed, dict):
                    paths = [p for p in parsed.get("reference_images", []) if p]
                else:
                    paths = []
                if paths:
                    found_count = sum(1 for p in paths if os.path.exists(p))
                    print("[VEO InputScan] Primary (JSON): {} image(s), {} on disk".format(
                        len(paths), found_count))
                    return paths
                else:
                    print("[VEO InputScan] Step 1: JSON parsed but empty list")
            else:
                print("[VEO InputScan] Step 1: knob value is blank/empty")
        except Exception as e:
            print("[VEO InputScan] JSON knob parse error: {}".format(e))
    else:
        print("[VEO InputScan] Step 1: knob not found on node")

    # Step 2: FALLBACK — scan input cache directory by generator name
    gen_name = _find_veo_generator(node)
    if gen_name:
        paths = _collect_veo_input_images_for_round(gen_name)
        if paths:
            return paths

    print("[VEO InputScan] No images found for '{}'".format(
        node.name() if node else "?"))
    return []
def _next_veo_name():
    """Return the next available name like 'veo1', 'veo2', etc."""
    used = set()
    for node in nuke.allNodes():
        n = node.name()
        if n == "veo":
            used.add(1)
        else:
            m = re.match(r"^veo(\d+)$", n, re.IGNORECASE)
            if m:
                used.add(int(m.group(1)))
    i = 1
    while i in used:
        i += 1
    return "veo{}".format(i)


def _create_veo_group_inputs(group_node, input_names):
    """Create Input nodes inside a VEO Group using NanoBanana's proven pattern.

    Pattern: REVERSE creation order + 'number' knob
    - Nuke: first-created Input = RIGHTmost on the DAG input strip.
    - We CREATE in REVERSE: rightmost name first -> leftmost last.
    - 'number' knob: higher value = more LEFT. Leftmost gets highest number.
    - Connection mapping: names[K] = node.input(count - 1 - K)

    Examples:
      Frames mode ["FirstFrame", "EndFrame"] (count=2):
        Created 1st: EndFrame   number=0  input(0)  RIGHT
        Created 2nd: FirstFrame number=1  input(1)  LEFT

      Ingredients mode ["img1", "img2", "img3"] (count=3):
        Created 1st: img3  number=0  input(0)  RIGHT
        Created 2nd: img2  number=1  input(1)
        Created 3rd: img1  number=2  input(2)  LEFT
    """
    count = len(input_names)
    group_node.begin()
    for i in range(count, 0, -1):
        inp = nuke.nodes.Input()
        label = input_names[i - 1]
        inp.setName(label)
        # number knob: leftmost = highest number
        inp["number"].setValue(count - i)
        # xpos: leftmost at 0, rightmost at (count-1)*200
        inp["xpos"].setValue((i - 1) * 200)
        inp["ypos"].setValue(0)
        print("[VEO DEBUG] _create_veo_inputs: '{}' num={} created_#{}"
              .format(inp.name(), count - i, count - i + 1))

    out = nuke.nodes.Output()
    out["xpos"].setValue(0)
    out["ypos"].setValue(200)
    group_node.end()

    # Debug verify
    for idx in range(count):
        print("[VEO DEBUG] _create_veo_inputs: verify input({}) -> {}"
              .format(idx, group_node.input(idx).name() if group_node.input(idx) else "None"))


def create_veo_node():
    """
    Create a VEO node.
    Auto-detect mode based on the number of selected nodes (any type, not just Read):
      - 0 nodes selected -> FirstFrame mode (1 input, default)
      - 1 node selected  -> FirstFrame mode (1 input)
      - 2 nodes selected -> Frames mode (2 inputs: first + last frame)
      - 3+ nodes selected -> Ingredients mode (3 inputs)

    nuke.selectedNodes() returns nodes in reverse selection order (last selected first),
    so we reverse the list to get the natural selection order:
      first selected = FirstFrame, second selected = EndFrame, etc.
    """
    # nuke.selectedNodes() returns in reverse selection order, so reverse it
    sel = list(reversed(nuke.selectedNodes()))
    input_nodes = sel  # use all selected nodes, not just Read

    # Determine mode based on selected node count
    node_count = len(input_nodes)
    if node_count == 0:
        auto_mode = VEO_MODE_FIRST_FRAME  # Default to FirstFrame
    elif node_count == 1:
        auto_mode = VEO_MODE_FIRST_FRAME
    elif node_count == 2:
        auto_mode = VEO_MODE_FRAMES
    else:
        auto_mode = VEO_MODE_INGREDIENTS
        input_nodes = input_nodes[:3]  # max 3 inputs

    # Map mode to mode combo index for saving state
    _MODE_TO_INDEX = {
        VEO_MODE_TEXT: 0,
        VEO_MODE_FIRST_FRAME: 1,
        VEO_MODE_FRAMES: 2,
        VEO_MODE_INGREDIENTS: 3,
    }

    # Input names per mode
    _INPUT_NAMES = {
        VEO_MODE_TEXT: [],
        VEO_MODE_FIRST_FRAME: ["FirstFrame"],
        VEO_MODE_FRAMES: ["FirstFrame", "EndFrame"],
        VEO_MODE_INGREDIENTS: ["img1", "img2", "img3"],
    }

    needed_inputs = VEO_MODE_INPUT_COUNTS.get(auto_mode, 0)
    input_names = _INPUT_NAMES.get(auto_mode, [])

    # Position: below first selected node, or center of DAG
    ref_node = input_nodes[0] if input_nodes else None

    # Create the main VEO Group node
    group_node = nuke.nodes.Group()
    group_node.setName(_next_veo_name())
    group_node["tile_color"].setValue(0x4169E1FF)  # Royal Blue

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

    # Build internal structure using NanoBanana pattern (reverse creation + number knob)
    _create_veo_group_inputs(group_node, input_names[:needed_inputs])

    # Connect selected nodes using NanoBanana mapping:
    #   input_names[K] = node.input(needed_inputs - 1 - K)
    #   input_nodes[0] -> leftmost port (FirstFrame / img1)
    for k, src_node in enumerate(input_nodes[:needed_inputs]):
        port_idx = needed_inputs - 1 - k
        print("[VEO DEBUG] setInput(port={}, node='{}') for '{}'"
              .format(port_idx, src_node.name(), input_names[k]))
        group_node.setInput(port_idx, src_node)

    # Add custom VEO tab
    tab = nuke.Tab_Knob("veo_tab", "VEO")
    group_node.addKnob(tab)

    custom_knob = nuke.PyCustom_Knob(
        "veo_ui", "",
        "ai_workflow.veo.VeoKnobWidget()"
    )
    custom_knob.setFlag(nuke.STARTLINE)
    group_node.addKnob(custom_knob)

    # Pre-save mode state so the UI widget restores the correct mode
    mode_idx = _MODE_TO_INDEX.get(auto_mode, 0)
    mode_knob = nuke.Int_Knob("veo_s_mode", "s_mode")
    mode_knob.setVisible(False)
    group_node.addKnob(mode_knob)
    group_node["veo_s_mode"].setValue(mode_idx)

    # Log mode selection
    mode_display = {
        VEO_MODE_TEXT: "Text",
        VEO_MODE_FIRST_FRAME: "FirstFrame",
        VEO_MODE_FRAMES: "Frames",
        VEO_MODE_INGREDIENTS: "Ingredients",
    }
    print("VEO: Created '{}' with auto-detected mode: {} (selected {} nodes)".format(
        group_node.name(), mode_display.get(auto_mode, auto_mode), node_count))

    return group_node

