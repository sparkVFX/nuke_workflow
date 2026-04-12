"""
AI Workflow Toolbar - Nuke left sidebar toolbar with AI generation tools.
Creates a toolbar button in Nuke's left panel with 5 sub-buttons.
"""

import nuke


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
    """Generate Video Seedance - creates a NoOp node as placeholder."""
    node = nuke.createNode("NoOp")
    node.setName("GenerateVideo_Seedance")
    node["label"].setValue("Generate Video\nSeedance")
    node["tile_color"].setValue(0xFF6347FF)
    nuke.message("Generate Video Seedance node created.")


def create_node_generate_video_kling():
    """Generate Video Kling - creates a NoOp node as placeholder."""
    node = nuke.createNode("NoOp")
    node.setName("GenerateVideo_Kling")
    node["label"].setValue("Generate Video\nKling")
    node["tile_color"].setValue(0x20B2AAFF)
    nuke.message("Generate Video Kling: coming soon!")


def open_settings():
    """Open the shared AI Workflow settings dialog (API key, temp directory)."""
    from ai_workflow.nanobanana import NanoBananaSettingsDialog
    dialog = NanoBananaSettingsDialog()
    dialog.exec_()


def _extract_clip_info(node):
    """Extract clip info from a Read node or a Player Group node (VEO/NB Player)."""
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
    return None


def send_selected_to_studio():
    """Send the selected Read/Player node(s) to Nuke Studio via socket."""
    import socket
    import json
    import struct

    selected = nuke.selectedNodes()
    if not selected:
        nuke.message("Please select one or more Read or Player nodes first.")
        return

    clips = []
    for node in selected:
        info = _extract_clip_info(node)
        if info:
            clips.append(info)

    if not clips:
        nuke.message("No valid Read or Player node selected (or no file path set).")
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
        "Nano Viewer",
        "ai_workflow.toolbar.create_nano_viewer()",
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
