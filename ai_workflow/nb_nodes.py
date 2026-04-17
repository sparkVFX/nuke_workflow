"""NanoBanana node creation functions.

Extracted from ``nanobanana.py`` for maintainability.  Contains all
functions that create or manipulate Nuke Group/Read/Dot nodes:

- ``create_nb_player_node`` — creates the Nano_Viewer Group + Read wrapper
- ``create_prompt_node`` — creates the legacy Prompt record node
- ``update_prompt_read_node`` — updates the Player/Read linked to a Prompt
- ``create_nanobanana_node`` — creates the NanoBanana_Generate Group node
- ``_create_group_inputs`` — creates Input/Output nodes inside a Group
- ``_nanobanana_input_changed`` — auto-expand Group inputs callback

Backward-compatible re-exports are added to ``nanobanana.py`` so that
existing code (``from ai_workflow.nanobanana import create_nb_player_node``)
continues to work.
"""

import nuke
import os
import json
import re

from ai_workflow.core.nuke_utils import (
    get_internal_read as _get_internal_read_nb,
    next_node_name as _next_node_name,
    rebuild_group_for_thumbnail as _rebuild_group_for_thumbnail,
    update_node_thumbnail as _update_node_thumbnail,
)

# ---------------------------------------------------------------------------
# Constants (shared with nanobanana.py)
# ---------------------------------------------------------------------------
MAX_INPUT_IMAGES = 14  # Absolute max (Gemini 3.1 Flash limit)
MODEL_MAX_INPUTS = {
    "gemini-3.1-flash-image-preview": 14,
    "gemini-3-pro-image-preview": 14,
    "gemini-2.5-flash-image": 3,
    "gemini-2.0-flash-exp-image-generation": 1,
    "imagen-3.0-generate-002": 1,
}


# ---------------------------------------------------------------------------
# Send to Studio helper
# ---------------------------------------------------------------------------
_SEND_TO_STUDIO_SCRIPT = """
import nuke, socket, json, struct

node = nuke.thisNode()
file_path = ""
if "nb_file" in node.knobs():
    file_path = node["nb_file"].value()
elif "file" in node.knobs():
    file_path = node["file"].value()
if not file_path:
    nuke.message("No file path set on this node.")
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


# ---------------------------------------------------------------------------
# NB Player Node Creation
# ---------------------------------------------------------------------------

def create_nb_player_node(image_path=None, name=None, xpos=None, ypos=None,
                         prompt="", neg_prompt="", model="",
                         ratio="auto", resolution="1K", seed=0,
                         input_images=None, gen_name=""):
    """
    Create a NanoBanana Player Group node wrapping a Read node.
    Similar to VEO Player but for single images (no frame range knobs).
    
    Now includes generation parameters and regeneration capability,
    replacing the old Prompt node pattern.

    Returns:
        tuple: (group_node, internal_read_node)
    """
    # Wrap entire node creation as a single undo unit so that
    # subsequent rename-undo cannot partially break internal structure
    nuke.Undo.begin("Create Nano Viewer")
    try:
        _default_name = _next_node_name("Nano_Viewer")
        group = nuke.nodes.Group(name=(name or _default_name))

        if xpos is not None:
            group["xpos"].setValue(int(xpos))
        if ypos is not None:
            group["ypos"].setValue(int(ypos))

        # Green colour (same as VEO Player)
        group["tile_color"].setValue(0x2E2E2EFF)
        # No label — keep the node name clean in the DAG

        # --- Build internals: Read → Output ---
        group.begin()
        read_node = nuke.nodes.Read(name="InternalRead")
        out_node = nuke.nodes.Output(name="Output")
        out_node.setInput(0, read_node)
        group.end()

        # Load the image file AFTER group.end() so knobs are fully populated
        if image_path and os.path.exists(image_path):
            group.begin()
            read_node["file"].fromUserText(image_path)
            group.end()

        # --- Expose Read-tab knobs on the Group panel ---
        # Use REAL knobs (NOT Link_Knob) so they survive rename-undo.
        # Link_Knob stores hardcoded TCL paths like "NodeName.InternalRead.format"
        # which break after undo-rename. Real knobs store actual values,
        # and a knobChanged callback keeps them synced via name lookup.
        tab_read = nuke.Tab_Knob("read_tab", "Read")
        group.addKnob(tab_read)

        # --- file knob ---
        file_knob = nuke.File_Knob("nb_file", "file")
        if image_path:
            file_knob.setValue(image_path.replace("\\", "/"))
        group.addKnob(file_knob)

        # Track which knobs need syncing between Group panel <-> internal Read
        _read_sync_knobs = []

        # --- format (dropdown, like native Read) ---
        # Format_Knob has no enumValues; use nuke.formats() to build dropdown.
        fmt_values = []
        try:
            for _f in nuke.formats():
                _fn = _f.name()
                if _fn:
                    fmt_values.append(_fn)
        except Exception:
            pass
        if not fmt_values:
            fmt_values = ["---"]
        fmt_current = "---"
        try:
            _fv = read_node["format"].value()
            print("[NB Player] format.value type={} repr={}".format(type(_fv).__name__, repr(_fv)))
            if hasattr(_fv, 'width') and _fv.width() > 0:
                fmt_current = '%dx%d' % (_fv.width(), _fv.height())
                print("[NB Player] format from width/height: {}".format(fmt_current))
            elif hasattr(_fv, 'name') and _fv.name():
                fmt_current = _fv.name()
                print("[NB Player] format from name: {}".format(fmt_current))
            else:
                print("[NB Player] format fallback: str={}".format(str(_fv)))
        except Exception as e:
            print("[NB Player] ERR format read: {}".format(e))
        # Fallback: if format is a preset name (not WxH), try PIL for actual image size
        _looks_like_preset = (
            fmt_current != "---"
            and ('x' not in fmt_current or not fmt_current.split('x')[0].strip().isdigit())
        )
        if _looks_like_preset:
            print("[NB Player] format '{}' looks like preset, trying PIL...".format(fmt_current))
            _w, _h = 0, 0
            try:
                import PIL.Image as _PIL
                _img = _PIL.open(image_path) if (image_path and os.path.exists(image_path)) else None
                if _img:
                    _w, _h = _img.size
                    _img.close()
                    print("[NB Player] PIL size: {}x{}".format(_w, _h))
            except Exception as e:
                print("[NB Player] ERR PIL: {}".format(e))
            if _w > 0 and _h > 0:
                fmt_current = '%dx%d' % (_w, _h)
                print("[NB Player] format final (PIL): {}".format(fmt_current))
            else:
                print("[NB Player] WARN PIL failed, keeping preset '{}'".format(fmt_current))
        # Ensure fmt_current is in the dropdown (images may have non-preset sizes like 1024x1024)
        if fmt_current and fmt_current not in fmt_values:
            fmt_values.append(fmt_current)
            print("[NB Player] added '{}' to format dropdown".format(fmt_current))
        format_knob = nuke.Enumeration_Knob("nb_format", "format", fmt_values)
        print("[NB Player] setting nb_format='{}'".format(fmt_current))
        format_knob.setValue(fmt_current)
        group.addKnob(format_knob)
        _read_sync_knobs.append(("nb_format", "format"))

        # --- colorspace (Input Transform), premultiplied, raw, auto_alpha ---
        # Use real knobs, NOT Link_Knob. colorspace uses Enumeration_Knob for dropdown.

        if "colorspace" in read_node.knobs():
            cs_label = read_node["colorspace"].label() or "colorspace"
            # Build dropdown from Read node's own colorspace enum values
            cs_values = []
            try:
                _cs_k = read_node["colorspace"]
                if hasattr(_cs_k, "values") and callable(_cs_k.values):
                    cs_values = list(_cs_k.values()) or []
                elif hasattr(_cs_k, "enumerationItems") and callable(_cs_k.enumerationItems):
                    cs_values = list(_cs_k.enumerationItems()) or []
            except Exception:
                pass
            if not cs_values:
                cs_values = ["default", "linear", "sRGB", "Gamma1.8", "Gamma2.2",
                             "Rec709", "ACEScg", "ALEXAV3LogC"]
            current_cs = str(read_node["colorspace"].value())
            if current_cs not in cs_values:
                cs_values.insert(0, current_cs)
            cs_knob = nuke.Enumeration_Knob("nb_colorspace", cs_label, cs_values)
            cs_knob.setValue(current_cs)
            cs_knob.setFlag(nuke.STARTLINE)
            group.addKnob(cs_knob)
            _read_sync_knobs.append(("nb_colorspace", "colorspace"))

        for kname in ["premultiplied", "raw", "auto_alpha"]:
            if kname in read_node.knobs():
                klabel = read_node[kname].label() or kname
                real_knob = nuke.Boolean_Knob("nb_" + kname, klabel)
                real_knob.setValue(int(read_node[kname].value()))
                real_knob.clearFlag(nuke.STARTLINE)
                group.addKnob(real_knob)
                _read_sync_knobs.append(("nb_" + kname, kname))

        # --- Button to open internal Read node's full properties ---
        open_read_script = (
            "n = nuke.thisNode()\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "if r:\n"
            "    nuke.show(r)\n"
        )
        open_btn = nuke.PyScript_Knob(
            "open_read_props", "Open Full Read Properties", open_read_script
        )
        open_btn.setFlag(nuke.STARTLINE)
        group.addKnob(open_btn)

        # --- knobChanged callback: sync ALL exposed knobs <-> internal Read ---
        # Uses name-based lookup (nuke.toNode) so it survives rename-undo.
        # DEBUG: logs to Nuke Script Editor console
        _sync_pairs_str = repr(_read_sync_knobs).replace("'", '"')
        kc_script = (
            "import nuke\n"
            "n = nuke.thisNode()\n"
            "k = nuke.thisKnob()\n"
            "kn = k.name()\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "if not r:\n"
            "    pass\n"
            "# NOTE: no 'return' - Nuke runs knobChanged as standalone script, not a function\n"
            "# File changed: load + pull fresh values from Read\n"
            "if kn == 'nb_file' and r:\n"
            "    # IMPORTANT: fromUserText must run inside group context\n"
            "    n.begin()\n"
            "    r['file'].fromUserText(k.value())\n"
            "    n.end()\n"
            "    import json as _json_mod, os as _os_mod\n"
            "    try:\n"
            "        _fv = r['format'].value()\n"
            "        _fn = ''\n"
            "        if hasattr(_fv, 'width') and _fv.width() > 0:\n"
            "            _fn = '%dx%d' % (_fv.width(), _fv.height())\n"
            "        elif isinstance(_fv, str) and _fv:\n"
            "            _fn = _fv\n"
            "        # Fallback: if format looks like a preset name (not WxH), use PIL\n"
            "        if _fn and ('x' not in _fn or not _fn.split('x')[0].strip().isdigit()):\n"
            "            try:\n"
            "                from PIL import Image as _PILImage\n"
            "                _fp = k.value().strip()\n"
            "                if _fp and _os_mod.path.isfile(_fp):\n"
            "                    _pil_img = _PILImage.open(_fp)\n"
            "                    _w, _h = _pil_img.size\n"
            "                    _pil_img.close()\n"
            "                    if _w > 0 and _h > 0:\n"
            "                        _fn = '%dx%d' % (_w, _h)\n"
            "            except Exception:\n"
            "                pass\n"
            "        if _fn:\n"
            "            _cur = list(n['nb_format'].values())\n"
            "            if _fn not in _cur:\n"
            "                n['nb_format'].setValues(_cur + [_fn])\n"
            "            n['nb_format'].setValue(_fn)\n"
            "    except Exception:\n"
            "        pass\n"
            "    try:\n"
            "        _cv = str(r['colorspace'].value())\n"
            "        n['nb_colorspace'].setValue(_cv)\n"
            "    except Exception:\n"
            "        pass\n"
            "    # Force postage stamp refresh after file change\n"
            "    if 'postage_stamp' in n.knobs():\n"
            "        n['postage_stamp'].setValue(True)\n"
            "    # Force pixel computation so postage stamp has data to render\n"
            "    try:\n"
            "        n.sample('red', 0, 0)\n"
            "    except Exception:\n"
            "        pass\n"
            "# Sync Group->Read for all other exposed knobs\n"
            "_pairs = " + _sync_pairs_str + "\n"
            "for _gk, _rk in _pairs:\n"
            "    if kn == _gk and r and _rk in r.knobs():\n"
            "        try:\n"
            "            if _rk == 'format':\n"
            "                n.begin()\n"
            "                r['format'].setValue(k.value())\n"
            "                n.end()\n"
            "            elif isinstance(r[_rk].value(), int):\n"
            "                n.begin()\n"
            "                r[_rk].setValue(int(k.value()))\n"
            "                n.end()\n"
            "            else:\n"
            "                n.begin()\n"
            "                r[_rk].setValue(k.value())\n"
            "                n.end()\n"
            "        except Exception:\n"
            "            pass\n"
        )
        group["knobChanged"].setValue(kc_script)
        # --- Divider + Send to Studio button ---
        studio_divider = nuke.Text_Knob("studio_divider", "")
        group.addKnob(studio_divider)

        studio_btn = nuke.PyScript_Knob("send_to_studio", "Send To Sequence", _SEND_TO_STUDIO_SCRIPT)
        studio_btn.setFlag(nuke.STARTLINE)
        group.addKnob(studio_btn)

        # ============================================================
        # Generation parameters + Regenerate Tab (replaces old Prompt node)
        # ============================================================
        tab_gen = nuke.Tab_Knob("gen_tab", "Regenerate")
        group.addKnob(tab_gen)

        # Store generation settings as hidden knobs (read by PyCustom widget)
        model_knob = nuke.String_Knob("nb_model", "Model")
        model_knob.setValue(model or "")
        model_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(model_knob)

        ratio_knob = nuke.String_Knob("nb_ratio", "Ratio")
        ratio_knob.setValue(ratio or "")
        ratio_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(ratio_knob)

        res_knob = nuke.String_Knob("nb_resolution", "Resolution")
        res_knob.setValue(resolution or "")
        res_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(res_knob)

        seed_val = int(seed) if seed else 0
        seed_clamped = min(seed_val, 2147483647)
        seed_knob = nuke.Int_Knob("nb_seed", "Seed")
        seed_knob.setValue(seed_clamped)
        seed_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(seed_knob)

        prompt_knob = nuke.Multiline_Eval_String_Knob("nb_prompt", "Prompt")
        prompt_knob.setValue(prompt)
        prompt_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(prompt_knob)

        neg_knob = nuke.Multiline_Eval_String_Knob("nb_neg_prompt", "Negative Prompt")
        neg_knob.setValue(neg_prompt)
        neg_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(neg_knob)

        output_path_knob = nuke.File_Knob("nb_output_path", "Output Path")
        output_path_knob.setValue((image_path or "").replace("\\", "/"))
        output_path_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(output_path_knob)

        # Store input reference images as JSON (for Regenerate panel ImageStrip)
        input_img_paths = []
        if input_images:
            for img in (input_images if isinstance(input_images, list) else []):
                if isinstance(img, dict):
                    p = img.get("path", "")
                else:
                    p = str(img)
                if p:
                    input_img_paths.append(p)
        print("[NB Player] nb_input_images: storing {} paths".format(len(input_img_paths)))
        for _ip in input_img_paths:
            print("  [NB Player]   -> {}".format(_ip))
        # Use Multiline_Eval_String_Knob instead of String_Knob to avoid
        # the ~256 char length limit that truncates long JSON arrays of
        # file paths (especially temp-dir paths on Windows).
        # CRITICAL: setValue MUST be called AFTER addKnob, otherwise Nuke
        # resets the value when addKnob is called!
        inputs_json_knob = nuke.Multiline_Eval_String_Knob("nb_input_images", "Input Images")
        inputs_json_knob.setFlag(nuke.INVISIBLE)
        group.addKnob(inputs_json_knob)
        inputs_json_knob.setValue(json.dumps(input_img_paths))

        # Store generator node name so we can find cached input images later
        if gen_name:
            gen_name_knob = nuke.String_Knob("nb_gen_name", "Generator Name")
            gen_name_knob.setFlag(nuke.INVISIBLE)
            group.addKnob(gen_name_knob)
            gen_name_knob.setValue(gen_name)

        # PyCustom_Knob for the regenerate UI widget
        regen_custom = nuke.PyCustom_Knob(
            "nanobanana_regen_ui",
            "",
            "ai_workflow.nanobanana.NanoBananaPlayerRegenWidget()"
        )
        regen_custom.setFlag(nuke.STARTLINE)
        group.addKnob(regen_custom)

        # --- Hidden marker knob ---
        marker = nuke.Boolean_Knob("is_nb_player", "")
        marker.setValue(True)
        marker.setFlag(nuke.INVISIBLE)
        group.addKnob(marker)

        # Set colorspace to sRGB for generated images
        try:
            read_node["colorspace"].setValue("sRGB")
        except Exception:
            pass

        # --- Set thumbnail icon on the Group node (like Read's postage stamp) ---
        _update_node_thumbnail(group, image_path)

        return group, read_node
    finally:
        nuke.Undo.end()


# ---------------------------------------------------------------------------
# Prompt Node Creation and Management
# ---------------------------------------------------------------------------
def create_prompt_node(generator_node, prompt, neg_prompt, model, ratio, resolution, seed, 
                       output_image_path, images_info=None):
    """
    Create a NanoBanana_Prompt node that records the generation settings
    and allows regeneration.
    
    Args:
        generator_node: The NanoBanana_Generate node
        prompt: The prompt text
        neg_prompt: Negative prompt
        model: Model ID
        ratio: Aspect ratio
        resolution: Resolution
        seed: Seed used
        output_image_path: Path to the generated image
        images_info: List of input image info dicts
    
    Returns:
        tuple: (prompt_node, player_node)
    """
    # Get position of generator node
    gen_x = generator_node["xpos"].value()
    gen_y = generator_node["ypos"].value()
    gen_name = generator_node.name()
    
    # Find existing prompt nodes that belong to THIS generator
    # Walk the chain starting from generator_node's dependent nodes
    existing_prompts = []
    
    def find_prompt_chain(start_node):
        """Recursively find all Prompt nodes connected downstream from start_node."""
        for node in nuke.allNodes("Group"):
            if node.name().startswith("Prompt") and "nanobanana_prompt_tab" in node.knobs():
                # Check if this prompt's input 0 connects back to start_node
                inp = node.input(0)
                if inp and (inp.name() == start_node.name()):
                    existing_prompts.append(node)
                    find_prompt_chain(node)
    
    find_prompt_chain(generator_node)
    
    # Also check for prompts that store this generator's name
    # (for prompts that might have been reconnected)
    for node in nuke.allNodes("Group"):
        if node.name().startswith("Prompt") and "nanobanana_prompt_tab" in node.knobs():
            if "nb_gen_name" in node.knobs():
                if node["nb_gen_name"].value() == gen_name:
                    if node not in existing_prompts:
                        existing_prompts.append(node)
    
    # Calculate position for new prompt node (below generator or last prompt)
    if existing_prompts:
        # Find the last prompt node (furthest down)
        last_prompt = max(existing_prompts, key=lambda n: n["ypos"].value())
        prompt_x = last_prompt["xpos"].value()
        prompt_y = last_prompt["ypos"].value() + 150
        # Connect to last prompt's output
        connect_to = last_prompt
    else:
        prompt_x = gen_x
        prompt_y = gen_y + 150
        connect_to = generator_node

    # --- Create a Dot node between generator/previous-prompt and this prompt ---
    dot_node = nuke.nodes.Dot()
    dot_x = int(prompt_x) + 34  # Dot is small, offset to center under parent
    if not existing_prompts:
        dot_y = int(gen_y) + 100
    else:
        dot_y = int(prompt_y) - 50
    dot_node["xpos"].setValue(dot_x)
    dot_node["ypos"].setValue(dot_y)
    dot_node.setInput(0, connect_to)

    # Create Prompt node (Group with custom UI)
    prompt_node = nuke.nodes.Group()
    prompt_num = len(existing_prompts) + 1
    prompt_node.setName("Prompt{}".format(prompt_num))
    prompt_node["tile_color"].setValue(0x8B5CF6FF)  # Purple color
    prompt_node["xpos"].setValue(int(prompt_x))
    prompt_node["ypos"].setValue(int(prompt_y))
    
    # Leave label empty – user requested no prompt text in label
    prompt_node["label"].setValue("")
    
    # Build internal structure
    prompt_node.begin()
    inp = nuke.nodes.Input(name="Input")
    out = nuke.nodes.Output(name="Output")
    out.setInput(0, inp)
    prompt_node.end()
    
    # Prompt node connects to the Dot
    prompt_node.setInput(0, dot_node)
    
    # Add custom tab with stored settings
    tab = nuke.Tab_Knob("nanobanana_prompt_tab", "NanoBanana Prompt")
    prompt_node.addKnob(tab)
    
    # Store generator node name (for finding cached input images on reload)
    # Unified name across all node types: nb_gen_name
    if generator_node:
        gk = nuke.String_Knob("nb_gen_name", "Generator Name")
        gk.setFlag(nuke.INVISIBLE)
        prompt_node.addKnob(gk)
        gk.setValue(generator_node.name())

    # Store generation settings in knobs (all hidden - shown in custom widget)
    model_knob = nuke.String_Knob("nb_model", "Model")
    model_knob.setValue(model)
    model_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(model_knob)
    
    ratio_knob = nuke.String_Knob("nb_ratio", "Ratio")
    ratio_knob.setValue(ratio)
    ratio_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(ratio_knob)
    
    res_knob = nuke.String_Knob("nb_resolution", "Resolution")
    res_knob.setValue(resolution)
    res_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(res_knob)
    
    # Seed - clamp to valid range for Int_Knob (max 2147483647)
    seed_knob = nuke.Int_Knob("nb_seed", "Seed")
    seed_clamped = min(int(seed), 2147483647)
    seed_knob.setValue(seed_clamped)
    seed_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(seed_knob)
    
    # Prompt text (hidden - shown in custom widget)
    prompt_knob = nuke.Multiline_Eval_String_Knob("nb_prompt", "Prompt")
    prompt_knob.setValue(prompt)
    prompt_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(prompt_knob)
    
    # Negative prompt (hidden - shown in custom widget)
    neg_knob = nuke.Multiline_Eval_String_Knob("nb_neg_prompt", "Negative")
    neg_knob.setValue(neg_prompt)
    neg_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(neg_knob)
    
    # Output image path (hidden)
    output_knob = nuke.File_Knob("nb_output_path", "Output")
    output_knob.setValue(output_image_path.replace("\\", "/"))
    output_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(output_knob)
    
    # Store input image paths as JSON
    input_paths = []
    if images_info:
        for img in images_info:
            if img.get("connected") and img.get("path"):
                input_paths.append(img["path"])
    
    print("NanoBanana: Saving {} input image paths to Prompt node".format(len(input_paths)))
    for p in input_paths:
        print("  - {}".format(p))
    
    # CRITICAL: setValue MUST be called AFTER addKnob, otherwise Nuke
    # resets the value when addKnob is called!
    inputs_json_knob = nuke.Multiline_Eval_String_Knob("nb_input_images", "Input Images (JSON)")
    inputs_json_knob.setFlag(nuke.INVISIBLE)
    prompt_node.addKnob(inputs_json_knob)
    inputs_json_knob.setValue(json.dumps(input_paths))

    # Store generator node name so we can find cached input images later
    if generator_node:
        gen_name_knob = nuke.String_Knob("nb_gen_name", "Generator Name")
        gen_name_knob.setFlag(nuke.INVISIBLE)
        prompt_node.addKnob(gen_name_knob)
        gen_name_knob.setValue(generator_node.name())
    # Add PyCustom_Knob for regenerate UI
    divider = nuke.Text_Knob("divider1", "")
    prompt_node.addKnob(divider)
    
    custom_knob = nuke.PyCustom_Knob(
        "nanobanana_prompt_ui",
        "",
        "ai_workflow.nanobanana.NanoBananaPromptKnobWidget(nuke.thisNode())"
    )
    custom_knob.setFlag(nuke.STARTLINE)
    prompt_node.addKnob(custom_knob)
    
    # Create NB Player node (Group wrapping Read) for output image
    read_node = None
    player_node = None
    if output_image_path and os.path.exists(output_image_path):
        print("[NB Prompt] create_nb_player_node with {} images_info".format(len(images_info or [])))
        player_node, read_node = create_nb_player_node(
            image_path=output_image_path,
            name="生成图像Prompt{}".format(prompt_num),
            xpos=prompt_x + 200,
            ypos=prompt_y,
            input_images=images_info,
        )
        
        # Store reference to Player node in Prompt node
        read_ref_knob = nuke.String_Knob("nb_read_node", "Read Node")
        read_ref_knob.setValue(player_node.name())
        read_ref_knob.setFlag(nuke.INVISIBLE)
        prompt_node.addKnob(read_ref_knob)
        
        # Player's input 0 connects to prompt node (Prompt → Player direction)
        player_node.setInput(0, prompt_node)
        
        print("NanoBanana: Created NB Player '{}' for output: {}".format(player_node.name(), output_image_path))
    else:
        print("NanoBanana: WARNING - No output image to create Player node. Path: {}".format(output_image_path))
        # Still add the read node reference knob (empty)
        read_ref_knob = nuke.String_Knob("nb_read_node", "Read Node")
        read_ref_knob.setValue("")
        read_ref_knob.setFlag(nuke.INVISIBLE)
        prompt_node.addKnob(read_ref_knob)
    
    return prompt_node, player_node


def update_prompt_read_node(prompt_node, new_image_path):
    """Update the NB Player (or legacy Read) node associated with a Prompt node.
    If the node doesn't exist, create a new NB Player."""
    if "nb_read_node" not in prompt_node.knobs():
        return None

    player_node_name = prompt_node["nb_read_node"].value()
    player_node = nuke.toNode(player_node_name) if player_node_name else None

    if player_node:
        # Check if it's an NB Player Group or a legacy Read node
        internal_read = _get_internal_read_nb(player_node)
        if internal_read:
            # It's an NB Player Group — update the internal Read
            internal_read["file"].fromUserText(new_image_path)
            # Also sync the Group's nb_file knob
            if "nb_file" in player_node.knobs():
                player_node["nb_file"].setValue(new_image_path.replace("\\", "/"))
        elif player_node.Class() == "Read":
            # Legacy Read node — update directly
            player_node["file"].fromUserText(new_image_path)
        # Update stored path
        if "nb_output_path" in prompt_node.knobs():
            prompt_node["nb_output_path"].setValue(new_image_path.replace("\\", "/"))
        # Update the node thumbnail — use Replacement Jutsu for Group nodes
        if player_node.Class() == "Group" and "is_nb_player" in player_node.knobs():
            rebuilt = _rebuild_group_for_thumbnail(player_node, new_image_path)
            if rebuilt:
                player_node = rebuilt
            else:
                _update_node_thumbnail(player_node, new_image_path)
        else:
            _update_node_thumbnail(player_node, new_image_path)
        return player_node

    # Node not found — create a new NB Player
    prompt_x = int(prompt_node["xpos"].value())
    prompt_y = int(prompt_node["ypos"].value())

    player_node, read_node = create_nb_player_node(
        image_path=new_image_path,
        name="regenerated_image",
        xpos=prompt_x + 200,
        ypos=prompt_y,
    )

    # Store new node reference
    if "nb_read_node" in prompt_node.knobs():
        prompt_node["nb_read_node"].setValue(player_node.name())
    else:
        read_ref_knob = nuke.String_Knob("nb_read_node", "Read Node")
        read_ref_knob.setValue(player_node.name())
        prompt_node.addKnob(read_ref_knob)

    if "nb_output_path" in prompt_node.knobs():
        prompt_node["nb_output_path"].setValue(new_image_path.replace("\\", "/"))

    # Player's input 0 connects to prompt node (Prompt → Player direction)
    player_node.setInput(0, prompt_node)

    print("NanoBanana: Created new NB Player '{}' for regenerated image: {}".format(
        player_node.name(), new_image_path))

    return player_node


# ---------------------------------------------------------------------------
# Group Input Management
# ---------------------------------------------------------------------------

def _create_group_inputs(group_node, count):
    """Create Input nodes inside a Group.

    TWO mechanisms work together for user's desired layout:

      DAG display:  img1(LEFT)  img2  ...  imgN(RIGHT)

    1) CREATION ORDER controls DAG visual position:
       Nuke: first-created Input = RIGHTmost, each new one goes LEFT.
       So we CREATE in REVERSE order (imgN first -> right, img1 last -> left).

    2) 'number' KNOB controls input index (connection):
       Nuke: higher number = MORE LEFT in DAG.
       We want img1 on the LEFT, so img1 gets the HIGHEST number.
       Mapping:  imgK -> number = (count - K)

    Summary table (for count=3):
      Name   | Created   | number | input idx | DAG pos
      ------ | --------- | ------ | --------- | --------
      img3   | 1st (→right) | 0     | 0         | RIGHTMOST
      img2   | 2nd         | 1     | 1         | middle
      img1   | 3rd (→left) | 2     | 2         | LEFTMOST
    """
    group_node.begin()
    # Reverse creation order: imgN first (rightmost), img1 last (leftmost)
    for i in range(count, 0, -1):
        inp = nuke.nodes.Input()
        inp.setName("img{}".format(i))
        # imgK gets number (count-K): img1=highest=leftmost, imgN=0=rightmost
        inp["number"].setValue(count - i)
        # xpos: img1 at left (0), imgN at right ((count-1)*100)
        inp["xpos"].setValue((i - 1) * 100)
        inp["ypos"].setValue(0)
        print("[NanoBanana] _create: '{}' number={} created_#{}'"
              .format(inp.name(), int(inp["number"].value()),
                      count - i + 1))
    
    out = nuke.nodes.Output()
    out["xpos"].setValue(0)
    out["ypos"].setValue(200)
    group_node.end()
    
    # Debug verify
    for idx in range(count):
        print("[NanoBanana] _create: verify input({}) -> {}'"
              .format(idx, group_node.input(idx)))


def create_nanobanana_node():
    """Create a NanoBanana_Generate node with multiple inputs using Group.

    - No nodes selected  -> start with 1 input  (img1 only)
    - 1+ nodes selected  -> start with 1 input (img1), auto-connect selected node to img1

    Auto-expands up to MAX_INPUT_IMAGES when all inputs are connected.

    DAG layout:       img1(LEFT)  img2  ...  imgN(RIGHT)
    Input mapping:     imgK = input index (count - K)
                       img1 = input(count-1) [leftmost, highest number]
    """
    sel = nuke.selectedNodes()
    sel_node = sel[0] if sel else None

    # Always start with 1 input; if node selected, connect it to img1 after creation
    initial_inputs = 1

    group_node = nuke.nodes.Group(name=_next_node_name("NanoBanana"))
    group_node["tile_color"].setValue(0x2E2E2EFF)

    # Position below selected node, or at DAG viewport center
    if sel_node:
        sx = int(sel_node["xpos"].value())
        sy = int(sel_node["ypos"].value())
        group_node["xpos"].setValue(sx)
        group_node["ypos"].setValue(sy + 100)
        print("[NanoBanana] create: positioned under '{}'".format(sel_node.name()))
    else:
        try:
            center = nuke.center()
            x, y = int(center[0]), int(center[1])
        except Exception:
            x, y = 0, 0
        group_node["xpos"].setValue(x)
        group_node["ypos"].setValue(y)
        print("[NanoBanana] create: positioned at DAG viewport center ({}, {})"
              .format(x, y))

    # Build internal Input / Output nodes (reverse creation + explicit number)
    _create_group_inputs(group_node, initial_inputs)

    # Auto-connect selected node to img1 (input index = count - 1 = 0 for count=1)
    if sel_node:
        img1_idx = initial_inputs - 1  # img1 maps to input(0) when count=1
        group_node.setInput(img1_idx, sel_node)
        print("[NanoBanana] create: connected '{}' -> img1 (input{})'"
              .format(sel_node.name(), img1_idx))

    # Add our custom NanoBanana tab FIRST (so no "User" tab is auto-created)
    tab = nuke.Tab_Knob("nanobanana_tab", "NanoBanana")
    group_node.addKnob(tab)

    custom_knob = nuke.PyCustom_Knob(
        "nanobanana_ui", "",
        "ai_workflow.nanobanana.NanoBananaKnobWidget()"
    )
    custom_knob.setFlag(nuke.STARTLINE)
    group_node.addKnob(custom_knob)

    # Track current input count for auto-expansion (hidden, under NanoBanana tab)
    count_knob = nuke.Int_Knob("nb_input_count", "Input Count")
    count_knob.setValue(initial_inputs)
    count_knob.setVisible(False)
    group_node.addKnob(count_knob)

    # Store per-model max inputs (default model is first in combo: gemini-3.1-flash)
    max_knob = nuke.Int_Knob("nb_max_inputs", "Max Inputs")
    max_knob.setValue(MODEL_MAX_INPUTS.get("gemini-3.1-flash-image-preview", MAX_INPUT_IMAGES))
    max_knob.setVisible(False)
    group_node.addKnob(max_knob)

    return group_node


_expanding_inputs = False  # Guard against recursive knobChanged callbacks


def _nanobanana_input_changed():
    """Callback: auto-expand Group inputs when all current inputs are connected.

    Target DAG:  img1(LEFT)  ...  imgN(RIGHT)
    Mapping:     imgK = input index (new_count - K)

    Steps:
      1. Save connections: imgK -> node.input(current_count - K)
      2. Delete ALL Input nodes
      3. Recreate in reverse order with number knobs:
           img(N+1) first (rightmost), ..., img1 last (leftmost)
           each gets number = new_count - K
      4. Restore connections via new mapping
    """
    global _expanding_inputs
    if _expanding_inputs:
        return

    node = nuke.thisNode()
    if not _is_generator_node(node):
        return
    if node.Class() != "Group":
        return
    if "nb_input_count" not in node.knobs():
        return
    
    # Use per-model max if available, otherwise global max
    if "nb_max_inputs" in node.knobs():
        max_allowed = int(node["nb_max_inputs"].value())
    else:
        max_allowed = MAX_INPUT_IMAGES

    current_count = int(node["nb_input_count"].value())
    if current_count >= max_allowed:
        return
    
    # Debug log
    print("[NanoBanana] _input_changed: '{}' count={}/max={}"
          .format(node.name(), current_count, max_allowed))
    for i in range(current_count):
        conn = node.input(i)
        print("[NanoBanana]   input({}) <- {}'"
              .format(i, conn.name() if conn else "None"))

    # Check if all connected
    all_connected = True
    for i in range(current_count):
        if node.input(i) is None:
            all_connected = False
            break
    
    if all_connected:
        _expanding_inputs = True
        try:
            new_count = current_count + 1
            print("[NanoBanana]   EXPAND {} -> {}".format(current_count, new_count))

            # --- Step 1: Save connections ---
            # Old mapping: imgK = input (current_count - K)
            saved = {}
            for k in range(1, current_count + 1):
                old_idx = current_count - k
                conn = node.input(old_idx)
                if conn is not None:
                    saved[k] = conn
                    print("[NanoBanana]   save: img{} <- '{}'"
                          .format(k, conn.name()))

            # --- Step 2: Delete all Inputs ---
            node.begin()
            del_names = [n.name() for n in nuke.allNodes("Input")]
            for inp in list(nuke.allNodes("Input")):
                nuke.delete(inp)
            node.end()
            print("[NanoBanana]   delete: {}".format(del_names))

            # --- Step 3: Recreate reverse order + number knob ---
            # imgK -> number = (new_count - K): img1=highest=leftmost
            node.begin()
            for i in range(new_count, 0, -1):
                inp = nuke.nodes.Input()
                inp.setName("img{}".format(i))
                inp["number"].setValue(new_count - i)
                inp["xpos"].setValue((i - 1) * 100)
                inp["ypos"].setValue(0)
                print("[NanoBanana]   create: '{}' num={} #{}'"
                      .format(inp.name(), int(inp["number"].value()),
                              new_count - i + 1))
            node.end()

            # --- Step 4: Restore ---
            # New mapping: imgK = input (new_count - K)
            # Track which indices we intentionally set
            set_indices = set()
            for k, conn_node in saved.items():
                new_idx = new_count - k
                node.setInput(new_idx, conn_node)
                set_indices.add(new_idx)
                print("[NanoBanana]   restore: img{} <- input {} ('{}')"
                      .format(k, new_idx, conn_node.name()))

            # --- Step 5: Clear auto-filled inputs ---
            # Nuke's setInput(N, node) auto-fills indices 0..N-1 with same node.
            # Clear any index that we didn't explicitly set above.
            for i in range(new_count):
                if i not in set_indices:
                    print("[NanoBanana]   clear auto-fill: input({})".format(i))
                    node.setInput(i, None)

            node["nb_input_count"].setValue(new_count)

            # Verify
            print("[NanoBanana]   AFTER ({} inputs):".format(new_count))
            for i in range(new_count):
                c = node.input(i)
                print("[NanoBanana]     input({}) <- {}".format(
                    i, c.name() if c else "None"))
            node.begin()
            for inp in nuke.allNodes("Input"):
                print("[NanoBanana]     internal '{}' number={}"
                      .format(inp.name(), int(inp["number"].value())))
            node.end()
        finally:
            _expanding_inputs = False


def _is_generator_node(node):
    """Return True if *node* is a NanoBanana Generate node (not a Player/Prompt)."""
    name = node.name()
    # Match "NanoBanana", "NanoBanana1", "NanoBanana2", ... AND legacy "NanoBanana_Generate*"
    # But NOT "Nano_Viewer*" (player nodes)
    if name.startswith("NanoBanana_Generate"):
        return True
    if name == "NanoBanana" or re.match(r"^NanoBanana\d+$", name):
        return True
    return False


def get_nanobanana_node():
    """Try to find the NanoBanana_Generate node."""
    # First check if there's a selected node
    selected = nuke.selectedNodes()
    for node in selected:
        if _is_generator_node(node):
            return node
    
    # Search all nodes for NanoBanana_Generate
    for node in nuke.allNodes():
        if _is_generator_node(node):
            return node
    return None


# Register the callback for auto-expanding inputs
nuke.addKnobChanged(_nanobanana_input_changed, nodeClass="Group")
