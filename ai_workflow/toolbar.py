"""
AI Workflow Toolbar - Nuke left sidebar toolbar with AI generation tools.
Creates a toolbar button in Nuke's left panel with 5 sub-buttons.
"""

import nuke
import os
import tempfile


def create_node_generate_image_midjourney():
    """Generate Image Midjourney - creates a NoOp node as placeholder."""
    node = nuke.createNode("NoOp")
    node.setName("GenerateImage_Midjourney")
    node["label"].setValue("Generate Image\nMidjourney")
    node["tile_color"].setValue(0x7B68EEFF)
    nuke.message("Generate Image Midjourney node created.")


def open_gemini_dialogue():
    """Open the Gemini Dialogue chat panel (floating window, not a node)."""
    import ai_workflow.gemini_chat as gc
    gc.open_gemini_chat_panel()


def create_node_generate_image_nanobanana():
    """Generate Image NanoBanana - creates a node with custom PySide/Qt panel."""
    import ai_workflow.nanobanana as nb
    nb.create_nanobanana_node()


def create_node_generate_video_veo():
    """Generate Video VEO - creates a VEO node with Dot inputs for first/last frame and reference."""
    import ai_workflow.veo as veo
    veo.create_veo_node()


def create_node_generate_video_seedance():
    """Generate Video Seedance - creates a Seedance node with full UI."""
    import ai_workflow.seedance as sd
    sd.create_seedance_node()


def create_node_generate_video_kling():
    """Generate Video Kling - creates a NoOp node as placeholder."""
    node = nuke.createNode("NoOp")
    node.setName("GenerateVideo_Kling")
    node["label"].setValue("Generate Video\nKling")
    node["tile_color"].setValue(0x20B2AAFF)
    nuke.message("Generate Video Kling: coming soon!")


def open_settings():
    """Open the shared AI Workflow settings dialog (API key, temp directory)."""
    from ai_workflow.core.settings import AppSettings
    from ai_workflow.nanobanana import NanoBananaSettingsDialog
    dialog = NanoBananaSettingsDialog()
    dialog.exec_()


def _render_node_output(node, render_all_frames=False):
    """Render a node's output to a temporary file.

    For images or single-frame: renders as PNG.
    For video with render_all_frames=True: renders full frame range as MOV.
    For video with render_all_frames=False: renders current frame as PNG.

    Args:
        node: The Nuke node to render.
        render_all_frames: If True, render the entire frame range as video.
                           If False, render only the current frame.

    Returns:
        File path of the rendered temp file, or None on failure.
    """
    from ai_workflow.core.rendering import render_input_to_file_silent

    # Determine temp directory (use settings if available)
    try:
        from ai_workflow.core.directories import get_temp_directory
        temp_dir = get_temp_directory()
    except Exception:
        temp_dir = tempfile.gettempdir()

    # Determine frame range
    first_frame = nuke.frame()
    last_frame = first_frame

    # Try to detect frame range from upstream Read/Group
    is_video = False
    try:
        _up = node
        for _depth in range(20):  # walk up to 20 nodes looking for a Read
            if _up is None:
                break
            if _up.Class() == "Read":
                _f = int(_up["first"].value()) if "first" in _up.knobs() else first_frame
                _l = int(_up["last"].value()) if "last" in _up.knobs() else last_frame
                if _l > _f:
                    is_video = True
                    first_frame = _f
                    last_frame = _l
                break
            elif _up.Class() == "Group":
                try:
                    _up.begin()
                    _inner_read = nuke.toNode("InternalRead")
                    _up.end()
                    if _inner_read:
                        _f = int(_inner_read["first"].value()) if "first" in _inner_read.knobs() else first_frame
                        _l = int(_inner_read["last"].value()) if "last" in _inner_read.knobs() else last_frame
                        if _l > _f:
                            is_video = True
                            first_frame = _f
                            last_frame = _l
                        break
                except Exception:
                    pass
            _up = _up.input(0)
    except Exception:
        pass

    node_name = node.name()

    if is_video and render_all_frames:
        # Render full range as MOV
        import time
        ext = "mov"
        filename = "{}_{}_{}".format(node_name, "allframes", int(time.time()))
        output_path = os.path.join(temp_dir, filename + "." + ext)

        try:
            write = nuke.nodes.Write()
            write.setInput(0, node)
            write["file"].setValue(output_path.replace("\\", "/"))
            write["file_type"].setValue("mov")
            write["channels"].setValue("rgba")
            # Hide Write node
            write["xpos"].setValue(-10000)
            write["ypos"].setValue(-10000)

            nuke.execute(write, first_frame, last_frame)
            nuke.delete(write)

            if os.path.exists(output_path):
                return output_path
        except Exception as e:
            print("[SendToSequence] Error rendering video: {}".format(e))
            try:
                nuke.delete(write)
            except Exception:
                pass
        return None
    else:
        # Render single frame as PNG
        filename = "{}_frame{}_{}".format(node_name, first_frame, __import__("time").time())
        output_path = os.path.join(temp_dir, filename + ".png")

        if render_input_to_file_silent(node, output_path, first_frame):
            return output_path
        return None


def _extract_clip_info(node):
    """Extract clip info from a Read node or a Player Group node (VEO/NB Player).

    For other node types (e.g. ColorCorrect, Grade), returns None so that
    send_selected_to_studio can fall back to rendering.
    """
    if node.Class() == "Read":
        file_path = node["file"].value()
        if file_path:
            return {"file": file_path, "name": node.name()}
    elif node.Class() == "Group":
        # VEO Player or NB Player — get file from internal Read
        try:
            node.begin()
            internal_read = nuke.toNode("InternalRead")
            node.end()
            if internal_read:
                file_path = internal_read["file"].value()
                if file_path:
                    return {"file": file_path, "name": node.name()}
        except Exception:
            pass
    # Other node types (ColorCorrect, Grade, etc.) → return None,
    # caller will handle via _render_node_output()
    return None


def send_selected_to_studio():
    """Send the selected node(s) to Nuke Studio via socket.

    Supports:
    - Read / Player Group nodes: sends original file path directly.
    - Any other node (e.g. ColorCorrect): renders output to temp file, then sends.
    """
    import socket
    import json
    import struct

    selected = nuke.selectedNodes()
    if not selected:
        nuke.message("Please select one or more nodes first.")
        return

    # Check if any selected node needs rendering (non-Read, non-Player)
    has_render_nodes = False
    has_direct_nodes = False
    for node in selected:
        info = _extract_clip_info(node)
        if info:
            has_direct_nodes = True
        else:
            has_render_nodes = True

    # Ask user about render mode only when there are nodes that need rendering
    render_all = False
    if has_render_nodes:
        if has_direct_nodes:
            # Mixed selection: direct + render nodes
            choice = nuke.ask(
                "Selection contains both media nodes and processing nodes.\n\n"
                "Processing nodes (ColorCorrect, etc.) need to be rendered.\n\n"
                "Click OK to render CURRENT FRAME only.\n"
                "Click Cancel to render FULL FRAME RANGE (video)."
            )
            if choice is None:
                render_all = True  # Cancel → render all frames
            # OK → render_all stays False (current frame only)
        else:
            # All nodes need rendering
            choice = nuke.ask(
                "Selected node(s) will be RENDERED before sending.\n\n"
                "Click OK to send CURRENT FRAME only (fast).\n"
                "Click Cancel to send FULL FRAME RANGE as video (slow)."
            )
            if choice is None:
                render_all = True

    clips = []
    for node in selected:
        info = _extract_clip_info(node)
        if info:
            clips.append(info)
        else:
            # Need to render this node
            temp_path = _render_node_output(node, render_all_frames=render_all)
            if temp_path:
                clips.append({"file": temp_path, "name": node.name()})

    if not clips:
        nuke.message("No valid clips to send.")
        return

    data = json.dumps({
        "action": "add_clips",
        "clips": clips,
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
        nuke.message("Failed to send to Studio: " + str(e))


def create_veo_player():
    """Manually create a VEO Player node for debugging/testing."""
    import ai_workflow.veo as veo
    player, read = veo.create_veo_player_node()
    return player


def create_nano_viewer():
    """Manually create a Nano Viewer (image viewer) Group node."""
    import ai_workflow.nanobanana as nb
    # Position at DAG center or below selected node
    sel = nuke.selectedNodes()
    sel_node = sel[0] if sel else None
    if sel_node:
        xpos = int(sel_node["xpos"].value())
        ypos = int(sel_node["ypos"].value()) + 100
    else:
        try:
            center = nuke.center()
            xpos, ypos = int(center[0]), int(center[1])
        except Exception:
            xpos, ypos = 0, 0
    player, read = nb.create_nb_player_node(xpos=xpos, ypos=ypos)
    return player


def create_veo_viewer():
    """Manually create a VEO Viewer (video viewer) Group node."""
    import ai_workflow.veo as veo
    # Position at DAG center or below selected node
    sel = nuke.selectedNodes()
    sel_node = sel[0] if sel else None
    if sel_node:
        xpos = int(sel_node["xpos"].value())
        ypos = int(sel_node["ypos"].value()) + 100
    else:
        try:
            center = nuke.center()
            xpos, ypos = int(center[0]), int(center[1])
        except Exception:
            xpos, ypos = 0, 0
    viewer, read = veo.create_veo_viewer_standalone(xpos=xpos, ypos=ypos)
    return viewer


def register_toolbar():
    """Register the AI Workflow toolbar in Nuke's left sidebar."""

    # Create a new toolbar in the Nodes toolbar (left sidebar)
    toolbar = nuke.toolbar("Nodes")
    ai_menu = toolbar.addMenu("CompMind", icon="CompMind_Logo.png")

    # Add the buttons to the toolbar menu
    ai_menu.addCommand(
        "Generate Dialogue Gemini",
        "ai_workflow.toolbar.open_gemini_dialogue()",
        icon="Gemini.png",
    )
    ai_menu.addCommand(
        "Generate Image NanoBanana",
        "ai_workflow.toolbar.create_node_generate_image_nanobanana()",
        icon="Banana.png",
    )
    ai_menu.addCommand(
        "Generate Video VEO",
        "ai_workflow.toolbar.create_node_generate_video_veo()",
        icon="VEO.png",
    )
    ai_menu.addCommand(
        "Generate Video Seedance",
        "ai_workflow.toolbar.create_node_generate_video_seedance()",
        icon="Seedance.png",
    )
    ai_menu.addCommand(
        "Generate Video Kling",
        "ai_workflow.toolbar.create_node_generate_video_kling()",
        icon="Kling.png",
    )

    # --- CompMind Nodes Submenu (Viewer nodes) ---
    compmind_nodes = ai_menu.addMenu("CompMind Nodes", icon="CompMind_Logo.png")
    compmind_nodes.addCommand(
        "CM Nano Viewer",
        "ai_workflow.toolbar.create_nano_viewer()",
        icon="CompMind_Logo.png",
    )
    compmind_nodes.addCommand(
        "CM VEO Viewer",
        "ai_workflow.toolbar.create_veo_viewer()",
        icon="CompMind_Logo.png",
    )
    ai_menu.addCommand(
        "Media Library",
        "ai_workflow.media_browser.show_media_browser_panel()",
        icon="Banana.png",
    )
    ai_menu.addCommand(
        "Setting",
        "ai_workflow.toolbar.open_settings()",
        icon="Setting.png",
    )
    ai_menu.addCommand(
        "Send To Sequence",
        "ai_workflow.toolbar.send_selected_to_studio()",
        icon="SendTo.png",
    )
