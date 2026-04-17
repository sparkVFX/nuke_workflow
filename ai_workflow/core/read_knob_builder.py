"""Reusable Read-knob exposure utilities for Nuke Group nodes.

Provides a single function, ``add_read_knobs_to_group``, that:
  - Creates real (non-Link_Knob) Read-tab knobs on a Group node
  - Generates the ``knobChanged`` TCL callback that keeps Group <-> InternalRead in sync
  - Returns the ``_read_sync_knobs`` list so callers can extend it if needed

Also provides ``sync_frame_range_from_duration`` for the common
"duration × fps → frame range" fallback pattern used by VEO and NB.
"""

import nuke
import os


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_read_knobs_to_group(
    group,
    read_node,
    *,
    prefix="veo_",
    file_knob_name=None,       # defaults to prefix + "file"
    file_value=None,
    add_frame_range=True,
    add_mov_options=True,
    add_send_to_studio=False,
    send_to_studio_script="",
    add_open_read_props=True,
    debug_label="ReadKnobBuilder",
    extra_debug_in_not_found=False,
):
    """Add Read-tab knobs to a Group node and wire up knobChanged sync.

    Args:
        group: The Group node to add knobs to.
        read_node: The internal Read node (must already exist inside the Group).
        prefix: Prefix for Group-level knob names (e.g. "veo_" or "nb_").
        file_knob_name: Name for the file knob (defaults to prefix + "file").
        file_value: Initial value for the file knob.
        add_frame_range: If True, add first/last/frame_mode/frame/origfirst/origlast.
        add_mov_options: If True, add MOV Options section knobs.
        add_send_to_studio: If True, add Send to Studio button.
        send_to_studio_script: TCL script for the Send to Studio button.
        add_open_read_props: If True, add "Open Full Read Properties" button.
        debug_label: Label for debug log messages in knobChanged script.
        extra_debug_in_not_found: If True, dump all internal nodes when
            InternalRead is not found (useful for player node debugging).
    Returns:
        list: ``_read_sync_knobs`` — list of (group_knob_name, read_knob_name) pairs.
    """
    if file_knob_name is None:
        file_knob_name = prefix + "file"

    # --- Tab: Read ---
    tab_read = nuke.Tab_Knob("read_tab", "Read")
    group.addKnob(tab_read)

    # Track which knobs need syncing between Group panel <-> internal Read
    _read_sync_knobs = []

    # --- file knob (special: File_Knob) ---
    file_knob = nuke.File_Knob(file_knob_name, "file")
    if file_value:
        file_knob.setValue(file_value.replace("\\", "/"))
    group.addKnob(file_knob)

    # --- format (dropdown, like native Read) ---
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
        if hasattr(_fv, 'name') and _fv.name():
            fmt_current = _fv.name()
        elif hasattr(_fv, 'width') and _fv.width() > 0:
            fmt_current = '%dx%d' % (_fv.width(), _fv.height())
        else:
            fmt_current = str(_fv)
    except Exception:
        pass
    if fmt_current and fmt_current not in fmt_values:
        fmt_values.append(fmt_current)
    format_knob = nuke.Enumeration_Knob(prefix + "format", "format", fmt_values)
    format_knob.setValue(fmt_current)
    group.addKnob(format_knob)
    _read_sync_knobs.append((prefix + "format", "format"))

    if add_frame_range:
        # --- frame range knobs: first, last ---
        _add_int_knob(group, read_node, "first", prefix + "first", "first",
                       _read_sync_knobs, startline=True)
        _add_int_knob(group, read_node, "last", prefix + "last", "last",
                       _read_sync_knobs, startline=False)

        # --- frame_mode and frame ---
        _add_enum_knob(group, read_node, "frame_mode", prefix + "frame_mode", "frame mode",
                        _read_sync_knobs, startline=True)
        _add_int_knob(group, read_node, "frame", prefix + "frame", "frame",
                       _read_sync_knobs, startline=False)

        # --- origfirst, origlast ---
        _add_int_knob(group, read_node, "origfirst", prefix + "origfirst", "origfirst",
                       _read_sync_knobs, startline=True)
        _add_int_knob(group, read_node, "origlast", prefix + "origlast", "origlast",
                       _read_sync_knobs, startline=False)

    # --- on_error ---
    _add_enum_knob(group, read_node, "on_error", prefix + "on_error", "missing frames",
                    _read_sync_knobs, startline=True)

    # --- colorspace ---
    if "colorspace" in read_node.knobs():
        cs_label = read_node["colorspace"].label() or "colorspace"
        cs_values = _get_enum_values(read_node["colorspace"])
        if not cs_values:
            cs_values = ["default", "linear", "sRGB", "Gamma1.8", "Gamma2.2",
                         "Rec709", "ACEScg", "ALEXAV3LogC"]
        current_cs = str(read_node["colorspace"].value())
        if current_cs not in cs_values:
            cs_values.insert(0, current_cs)
        cs_knob = nuke.Enumeration_Knob(prefix + "colorspace", cs_label, cs_values)
        cs_knob.setFlag(nuke.STARTLINE)
        cs_knob.setValue(current_cs)
        group.addKnob(cs_knob)
        _read_sync_knobs.append((prefix + "colorspace", "colorspace"))

    # --- premultiplied, raw, auto_alpha ---
    for rk in ["premultiplied", "raw", "auto_alpha"]:
        if rk in read_node.knobs():
            rl = read_node[rk].label() or rk
            real_k = nuke.Boolean_Knob(prefix + rk, rl)
            real_k.setValue(int(read_node[rk].value()))
            real_k.clearFlag(nuke.STARTLINE)
            group.addKnob(real_k)
            _read_sync_knobs.append((prefix + rk, rk))

    if add_mov_options:
        _add_mov_options(group, read_node, prefix, _read_sync_knobs)

    # --- Button to open internal Read node's full properties ---
    if add_open_read_props:
        open_read_script = (
            "n = nuke.thisNode()\n"
            "n.begin()\n"
            "r = nuke.toNode('InternalRead')\n"
            "n.end()\n"
            "if r:\n"
            "    nuke.show(r)\n"
        )
        open_btn = nuke.PyScript_Knob("open_read_props", "Open Full Read Properties", open_read_script)
        open_btn.setFlag(nuke.STARTLINE)
        group.addKnob(open_btn)

    # --- knobChanged callback ---
    kc_script = _build_knobchanged_script(
        _read_sync_knobs, file_knob_name, prefix,
        debug_label=debug_label,
        extra_debug_in_not_found=extra_debug_in_not_found,
    )
    group["knobChanged"].setValue(kc_script)

    # --- Divider + Send to Studio button ---
    if add_send_to_studio:
        divider = nuke.Text_Knob("studio_divider", "")
        group.addKnob(divider)
        btn = nuke.PyScript_Knob("send_to_studio", "Send to Studio", send_to_studio_script)
        btn.setFlag(nuke.STARTLINE)
        group.addKnob(btn)

    return _read_sync_knobs


def sync_frame_range_from_duration(read_node, group_node=None, duration=None,
                                    prefix="veo_", tag="[SyncRange]",
                                    push_group_to_read=False):
    """Sync frame range on a Read node using a multi-tier fallback strategy.

    Tier 1: Use Read's own origfirst/origlast if valid (>1/1).
    Tier 1.5: (only if *push_group_to_read*) Push Group first/last → Read.
    Tier 2: Compute from duration × fps.
    Tier 3: Read the ``<prefix>duration`` knob from the Group node.

    After setting Read knobs, also syncs the corresponding Group-level knobs
    (``<prefix>first``, ``<prefix>last``, etc.) if *group_node* is provided.

    Args:
        read_node: The internal Read node to set frame range on.
        group_node: Optional Group node for knob fallback / sync.
        duration: Optional duration value (seconds, str or number).
        prefix: Prefix for Group-level knob names ("veo_" or "nb_").
        tag: Prefix for log messages.
        push_group_to_read: If True, when Read's origfirst/origlast are
            invalid (1/1), try pushing Group-level first/last → Read before
            falling back to duration.  Useful after nodeCopy/nodePaste where
            Group knobs retain correct values but Read may not.

    Returns:
        bool: True if frame range was successfully set.
    """
    _range_set = False

    # Tier 1: origfirst/origlast
    try:
        _of = int(read_node["origfirst"].value()) if "origfirst" in read_node.knobs() else None
        _ol = int(read_node["origlast"].value()) if "origlast" in read_node.knobs() else None
        if _of is not None and _ol is not None and _ol > _of:
            if "first" in read_node.knobs():
                read_node["first"].setValue(_of)
            if "last" in read_node.knobs():
                read_node["last"].setValue(_ol)
            _range_set = True
            print("{}   Read frame range: {}-{} (from origfirst/origlast)".format(tag, _of, _ol))
    except Exception:
        pass

    # Tier 1.5: Group knobs → Read (only when push_group_to_read=True)
    if not _range_set and push_group_to_read and group_node:
        try:
            _gf = int(group_node[prefix + "first"].value()) if (prefix + "first") in group_node.knobs() else 1
            _gl = int(group_node[prefix + "last"].value()) if (prefix + "last") in group_node.knobs() else 1
            if _gl > _gf:
                if "first" in read_node.knobs():
                    read_node["first"].setValue(_gf)
                if "last" in read_node.knobs():
                    read_node["last"].setValue(_gl)
                if "origfirst" in read_node.knobs():
                    read_node["origfirst"].setValue(_gf)
                if "origlast" in read_node.knobs():
                    read_node["origlast"].setValue(_gl)
                _range_set = True
                print("{}   pushed Group range {}-{} -> Read (fallback)".format(tag, _gf, _gl))
        except Exception:
            pass

    # Tier 2: duration × fps
    if not _range_set and duration:
        _range_set = _set_range_from_duration(read_node, duration, tag)

    # Tier 3: <prefix>duration knob on Group
    if not _range_set and group_node:
        try:
            dur_knob_name = prefix + "duration"
            if dur_knob_name in group_node.knobs():
                _node_dur = group_node[dur_knob_name].value()
                if _node_dur:
                    _range_set = _set_range_from_duration(read_node, _node_dur, tag)
        except Exception:
            pass

    # Sync Read -> Group knobs
    if _range_set and group_node:
        _sync_read_to_group(read_node, group_node, prefix)

    return _range_set


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add_int_knob(group, read_node, read_knob_name, group_knob_name, label,
                   sync_list, startline=True):
    """Add an Int_Knob mirroring a Read knob, if it exists."""
    if read_knob_name in read_node.knobs():
        try:
            k = nuke.Int_Knob(group_knob_name, label)
            k.setValue(int(read_node[read_knob_name].value()))
            if startline:
                k.setFlag(nuke.STARTLINE)
            else:
                k.clearFlag(nuke.STARTLINE)
            group.addKnob(k)
            sync_list.append((group_knob_name, read_knob_name))
        except Exception:
            pass


def _add_enum_knob(group, read_node, read_knob_name, group_knob_name, label,
                    sync_list, startline=True):
    """Add an Enumeration_Knob mirroring a Read knob, if it exists."""
    if read_knob_name in read_node.knobs():
        try:
            enum_values = _get_enum_values(read_node[read_knob_name])
            if not enum_values:
                enum_values = [""]
            k = nuke.Enumeration_Knob(group_knob_name, label, enum_values)
            k.setValue(str(read_node[read_knob_name].value()))
            if startline:
                k.setFlag(nuke.STARTLINE)
            else:
                k.clearFlag(nuke.STARTLINE)
            group.addKnob(k)
            sync_list.append((group_knob_name, read_knob_name))
        except Exception:
            pass


def _get_enum_values(knob):
    """Extract enum values from a Nuke Enumeration_Knob."""
    try:
        if hasattr(knob, "enums") and callable(knob.enums):
            vals = list(knob.enums())
            if vals:
                return vals
    except Exception:
        pass
    try:
        if hasattr(knob, "values") and callable(knob.values):
            vals = list(knob.values())
            if vals:
                return vals
    except Exception:
        pass
    try:
        if hasattr(knob, "enumerationItems") and callable(knob.enumerationItems):
            vals = list(knob.enumerationItems())
            if vals:
                return vals
    except Exception:
        pass
    return []


def _add_mov_options(group, read_node, prefix, sync_list):
    """Add MOV Options section knobs."""
    mov_knob_names = [
        "ycbcr_matrix", "mov_data_range",
        "first_track_only", "metadata", "noprefix", "match_key_format",
        "mov64_decode_codec", "mov_decode_codec",
        "video_codec_knob",
    ]
    mov_divider_added = False
    for mk in mov_knob_names:
        if mk in read_node.knobs():
            if not mov_divider_added:
                mov_div = nuke.Text_Knob("mov_divider", "MOV Options")
                group.addKnob(mov_div)
                mov_divider_added = True
            ml = read_node[mk].label() or mk
            try:
                real_mov_k = _create_mirror_knob(read_node, mk, prefix + mk, ml)
                if real_mov_k is not None:
                    group.addKnob(real_mov_k)
                    sync_list.append((prefix + mk, mk))
            except Exception:
                continue

    # If MOV loaded, show video codec as text display
    if mov_divider_added:
        try:
            codec_val = ""
            if "video_codec_knob" in read_node.knobs():
                codec_val = read_node["video_codec_knob"].value()
            elif "mov64_decode_codec" in read_node.knobs():
                codec_val = read_node["mov64_decode_codec"].value()
            if codec_val:
                codec_text = nuke.Text_Knob("video_codec_display", "Video Codec", str(codec_val))
                group.addKnob(codec_text)
        except Exception:
            pass


def _create_mirror_knob(read_node, read_knob_name, group_knob_name, label):
    """Create a Group-level knob that mirrors a Read knob's type and value."""
    mk_val = read_node[read_knob_name].value()
    knob_class = read_node[read_knob_name].Class()

    if knob_class in ("Enumeration_Knob",):
        enum_vals = _get_enum_values(read_node[read_knob_name])
        if not enum_vals:
            enum_vals = [str(mk_val)]
        k = nuke.Enumeration_Knob(group_knob_name, label, enum_vals)
        k.setValue(str(mk_val))
        return k
    elif knob_class in ("String_Knob", "File_Knob"):
        k = nuke.String_Knob(group_knob_name, label)
        k.setValue(str(mk_val))
        return k
    elif knob_class in ("WH_Knob",):
        k = nuke.String_Knob(group_knob_name, label)
        k.setValue(str(mk_val))
        return k
    elif isinstance(mk_val, int) or knob_class == "Boolean_Knob":
        k = nuke.Boolean_Knob(group_knob_name, label)
        k.setValue(int(mk_val))
        return k
    elif isinstance(mk_val, str) and len(str(mk_val)) < 256:
        k = nuke.String_Knob(group_knob_name, label)
        k.setValue(str(mk_val))
        return k
    else:
        return None  # Skip unsupported types


def _set_range_from_duration(read_node, duration, tag=""):
    """Compute frame range from duration × fps and set on Read node."""
    try:
        _fps = nuke.root()["fps"].value()
        if not _fps or _fps <= 0:
            _fps = 24.0
        _dur_str = str(duration).replace("s", "").strip()
        _dur_val = float(_dur_str)
        _last_frame = int(round(_dur_val * _fps))
        if _last_frame > 1:
            if "first" in read_node.knobs():
                read_node["first"].setValue(1)
            if "last" in read_node.knobs():
                read_node["last"].setValue(_last_frame)
            if "origfirst" in read_node.knobs():
                read_node["origfirst"].setValue(1)
            if "origlast" in read_node.knobs():
                read_node["origlast"].setValue(_last_frame)
            return True
    except Exception:
        pass
    return False


def _sync_read_to_group(read_node, group_node, prefix="veo_"):
    """Sync Read knob values to corresponding Group-level knobs."""
    for _rk, _gk in [("first", prefix + "first"), ("last", prefix + "last"),
                      ("origfirst", prefix + "origfirst"), ("origlast", prefix + "origlast")]:
        try:
            if _rk in read_node.knobs() and _gk in group_node.knobs():
                group_node[_gk].setValue(int(read_node[_rk].value()))
        except Exception:
            pass


def _build_knobchanged_script(sync_pairs, file_knob_name, prefix,
                               debug_label="ReadKnobBuilder",
                               extra_debug_in_not_found=False):
    """Build the TCL knobChanged callback script.

    The script syncs all exposed knobs between Group and InternalRead,
    and handles the special case of file changes (pull format/colorspace/range
    back from Read after loading a new file).
    """
    _sync_pairs_str = repr(sync_pairs).replace("'", '"')
    _frame_range_pairs = repr([
        ("first", prefix + "first"),
        ("last", prefix + "last"),
        ("origfirst", prefix + "origfirst"),
        ("origlast", prefix + "origlast"),
    ]).replace("'", '"')

    not_found_block = (
        "    print('[{label}-DEBUG] WARNING: InternalRead NOT FOUND!')\n"
        "    pass\n"
    ).format(label=debug_label)

    if extra_debug_in_not_found:
        not_found_block = (
            "    print('[{label}-DEBUG] WARNING: InternalRead NOT FOUND! All internal nodes:')\n"
            "    n.begin()\n"
            "    for _dn in nuke.allNodes():\n"
            "        print('  [{label}-DEBUG]   internal: %s (%s)' % (_dn.name(), _dn.Class()))\n"
            "    n.end()\n"
            "    pass\n"
        ).format(label=debug_label)

    kc_script = (
        "import nuke\n"
        "n = nuke.thisNode()\n"
        "k = nuke.thisKnob()\n"
        "kn = k.name()\n"
        "# ===== DEBUG: knobChanged fired =====\n"
        "_dbg = '[{label}-DEBUG] knobChanged: node=%s knob=%s value=%s' % (n.name(), kn, str(k.value())[:80])\n"
        "print(_dbg)\n"
        "n.begin()\n"
        "r = nuke.toNode('InternalRead')\n"
        "n.end()\n"
        "_dbg2 = '[{label}-DEBUG] InternalRead found=%s' % (r is not None)\n"
        "print(_dbg2)\n"
        "if not r:\n"
        "{not_found}"
        "# File changed: load into Read + pull fresh values\n"
        "if kn == '{file_knob}' and r:\n"
        "    n.begin()\n"
        "    r['file'].fromUserText(k.value())\n"
        "    n.end()\n"
        "    # Pull fresh format from Read\n"
        "    try:\n"
        "        _fv = r['format'].value()\n"
        "        _fmt_name = ''\n"
        "        if hasattr(_fv, 'name') and _fv.name():\n"
        "            _fmt_name = _fv.name()\n"
        "        elif hasattr(_fv, 'width') and _fv.width() > 0:\n"
        "            _fmt_name = '%dx%d' % (_fv.width(), _fv.height())\n"
        "        if _fmt_name and '{prefix}format' in n.knobs():\n"
        "            _cur_vals = list(n['{prefix}format'].values())\n"
        "            if _fmt_name not in _cur_vals:\n"
        "                _cur_vals.append(_fmt_name)\n"
        "                n['{prefix}format'].setValues(_cur_vals)\n"
        "            n['{prefix}format'].setValue(_fmt_name)\n"
        "    except Exception:\n"
        "        pass\n"
        "    # Pull fresh frame range from Read\n"
        "    for _frk, _fgk in {frame_range_pairs}:\n"
        "        try:\n"
        "            if _frk in r.knobs() and _fgk in n.knobs():\n"
        "                n[_fgk].setValue(int(r[_frk].value()))\n"
        "        except Exception:\n"
        "            pass\n"
        "    # Pull fresh colorspace from Read\n"
        "    try:\n"
        "        _cv = str(r['colorspace'].value())\n"
        "        n['{prefix}colorspace'].setValue(_cv)\n"
        "    except Exception:\n"
        "        pass\n"
        "    # Force postage stamp refresh\n"
        "    if 'postage_stamp' in n.knobs():\n"
        "        n['postage_stamp'].setValue(True)\n"
        "    try:\n"
        "        n.sample('red', 0, 0)\n"
        "    except Exception:\n"
        "        pass\n"
        "# Sync Group -> Read for all other exposed knobs\n"
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
        "            print('[{label}-DEBUG] Synced %s -> %s = %s' % (_gk, _rk, str(k.value())[:60]))\n"
        "        except Exception as _e:\n"
        "            print('[{label}-DEBUG] SYNC ERROR %s->%s: %s' % (_gk, _rk, _e))\n"
        "print('[{label}-DEBUG] knobChanged done for %s' % kn)\n"
    ).format(
        label=debug_label,
        file_knob=file_knob_name,
        prefix=prefix,
        not_found=not_found_block,
        frame_range_pairs=_frame_range_pairs,
    )
    return kc_script
