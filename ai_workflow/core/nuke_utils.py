"""
Nuke utility functions — node manipulation, thumbnail refresh, naming.
"""

import os
import re
import nuke

from ai_workflow.core.pyside_compat import QtWidgets, QtCore


def get_internal_read(group_node):
    """Get the internal Read node from a Group node (NB Player or VEO Player)."""
    if group_node is None or group_node.Class() != "Group":
        return None
    try:
        group_node.begin()
        r = nuke.toNode("InternalRead")
        group_node.end()
        return r
    except Exception:
        return None


def next_node_name(prefix):
    """Return the next available name like 'Prefix1', 'Prefix2', etc."""
    used = set()
    for node in nuke.allNodes():
        n = node.name()
        if n == prefix:
            used.add(1)
        else:
            m = re.match(r"^{}(\d+)$".format(re.escape(prefix)), n)
            if m:
                used.add(int(m.group(1)))
    i = 1
    while i in used:
        i += 1
    return "{}{}".format(prefix, i)


def rebuild_group_for_thumbnail(node, image_path=None, marker_knob="is_nb_player"):
    """'Replacement Jutsu' — rebuild the Group node to force thumbnail refresh.

    Nuke's Group-node postage-stamp cache is bound to the C++ node instance
    and cannot be flushed via any public Python / Tcl API. The only reliable
    way to make the DAG show a new thumbnail is to replace the node with an
    identical copy.

    Returns the **new** Group node, or None on failure.
    """
    if not node or node.Class() != "Group":
        return None
    if marker_knob not in node.knobs():
        return None

    _tag = "[Rebuild]"

    try:
        node_name = node.name()
        print("{} START for '{}'".format(_tag, node_name))

        nuke.Undo.begin("Rebuild Thumbnail")

        # Set image_path on InternalRead BEFORE copy
        if image_path and os.path.isfile(image_path):
            ir = get_internal_read(node)
            if ir:
                node.begin()
                ir["file"].fromUserText(image_path)
                node.end()
                # Sync Group-level file knob
                for knob_name in ["nb_file", "veo_file"]:
                    if knob_name in node.knobs():
                        node[knob_name].setValue(image_path.replace("\\", "/"))
                if "nb_output_path" in node.knobs():
                    node["nb_output_path"].setValue(image_path.replace("\\", "/"))

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

        # Copy to clipboard
        for n in nuke.allNodes():
            n.setSelected(False)
        node.setSelected(True)
        nuke.nodeCopy("%clipboard%")

        # Delete old node
        nuke.delete(node)

        # Paste from clipboard
        for n in nuke.allNodes():
            n.setSelected(False)
        nuke.nodePaste("%clipboard%")

        # Find pasted node
        new_node = None
        for n in nuke.selectedNodes():
            if n.Class() == "Group" and marker_knob in n.knobs():
                new_node = n
                break

        if not new_node:
            print("{} ERROR: Could not find pasted node!".format(_tag))
            return None

        # Restore name and position
        if new_node.name() != node_name:
            new_node["name"].setValue(node_name)
        new_node["xpos"].setValue(xpos)
        new_node["ypos"].setValue(ypos)

        # Restore connections
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

        # Ensure postage_stamp is ON
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


def update_node_thumbnail(node, image_path=None):
    """Enable the native Nuke postage-stamp preview on the Group node.

    If image_path is provided, ensures InternalRead has that file loaded.
    After enabling postage_stamp, forces a redraw so the thumbnail updates.
    """
    if not node:
        return

    # Load image into InternalRead
    if image_path and os.path.isfile(image_path):
        try:
            internal_read = get_internal_read(node)
            if internal_read:
                node.begin()
                internal_read["file"].fromUserText(image_path)
                node.end()
        except Exception as e:
            print("[Thumbnail] InternalRead load ERROR: {}".format(e))

    # Enable postage_stamp
    if "postage_stamp" in node.knobs():
        try:
            node["postage_stamp"].setValue(True)
        except Exception:
            pass

    # Force compute + refresh
    try:
        node.sample("red", 0, 0)
    except Exception:
        pass

    try:
        nuke.modified()
    except Exception:
        pass

    # Toggle postage_stamp to force re-render
    try:
        node["postage_stamp"].setValue(False)
        node["postage_stamp"].setValue(True)
    except Exception:
        pass

    # Deferred DAG view refresh
    try:
        def _deferred_refresh():
            app = QtWidgets.QApplication.instance()
            if app:
                for tw in app.topLevelWidgets():
                    for gv in tw.findChildren(QtWidgets.QGraphicsView):
                        scene = gv.scene()
                        if scene:
                            scene.update()
                            scene.invalidate(scene.sceneRect(), QtWidgets.QGraphicsScene.AllLayers)
                        vp = gv.viewport()
                        if vp:
                            vp.repaint()
            try:
                node.setSelected(False)
                node.setSelected(True)
                node.setSelected(False)
            except Exception:
                pass

        QtCore.QTimer.singleShot(300, _deferred_refresh)
    except Exception:
        pass


def restore_thumbnails(marker_knob="is_nb_player", file_knob="nb_file"):
    """Restore postage-stamp thumbnails for all player nodes in the current script."""
    count = 0
    for node in nuke.allNodes("Group"):
        if marker_knob in node.knobs():
            file_path = ""
            if file_knob in node.knobs():
                file_path = node[file_knob].value()
            if file_path and os.path.exists(file_path):
                update_node_thumbnail(node, file_path)
                count += 1
    print("[AI Workflow] Restored {} thumbnails".format(count))
