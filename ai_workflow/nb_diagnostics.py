"""NanoBanana diagnostic utilities for Nuke DAG thumbnail refresh debugging.

These functions are development/debugging tools, never called by production code.
They can be invoked manually from the Nuke Script Editor for troubleshooting.

Call examples:
    import ai_workflow.nb_diagnostics as nbd
    nbd.diagnose_visual_refresh_v5("Nano_Viewer9", "E:/path/to/image.jpg")
    nbd.test_thumbnail_refresh("Nano_Viewer7")
    nbd.restore_nb_thumbnails()
"""

import nuke
import os

from ai_workflow.core.pyside_compat import QtWidgets, QtCore, QtGui
from ai_workflow.core.nuke_utils import (
    get_internal_read as _get_internal_read_nb,
    rebuild_group_for_thumbnail as _rebuild_group_for_thumbnail,
    update_node_thumbnail as _update_node_thumbnail,
)


# ---------------------------------------------------------------------------
# Helper stubs used by some diagnostic techniques (no-ops)
# ---------------------------------------------------------------------------

def _for_node_begin(read_target, is_group):
    """No-op helper; caller manages node.begin()/node.end() context."""
    pass


def _for_node_end(is_group):
    """No-op helper; caller manages node.begin()/node.end() context."""
    pass


# ---------------------------------------------------------------------------
# Diagnostic functions
# ---------------------------------------------------------------------------

def diagnose_visual_refresh_v3(node_name=None, image_path=None):
    """V3 Diagnostic: New approaches based on V2 findings.
    
    Key finding from V2: Visual refresh TECHNIQUES WORK (old→black transition).
    Problem: Re-rendered thumbnail shows BLACK = wrong data at render time.
    Root cause hypothesis: Some techniques clear file mid-operation, 
    OR Nuke's postage_stamp renderer picks up stale/cleared state.
    
    V3 Strategy:
      A) Control test: Can a PLAIN Read node update its thumbnail live?
      B) Safe refresh: NEVER clear file, only set new value + trigger
      C) Execution-based: Force proper render via correct API
      D) Hash/dirty flag: Find Nuke's internal invalidation mechanism
      E) Qt event injection: Simulate real user interaction
    """
    import time as _time

    print("=" * 70)
    print("[V3] diagnose_visual_refresh_v3 START")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            for nd in nuke.allNodes("Read"): found = nd; break
        if not found:
            print("[V3] ERROR: No node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[V3] ERROR: '{}' not found!".format(node_name)); return

    is_group = (node.Class() == "Group")
    print("[V3] Target: '{}' (Class={})".format(node_name, node.Class()))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs(): image_path = node["nb_file"].value()
        elif "file" in node.knobs(): image_path = node["file"].value()
    if not os.path.isfile(image_path):
        print("[V3] WARNING: File missing: {}".format(repr(image_path)))

    internal_read = _get_internal_read_nb(node) if is_group else None
    read_target = internal_read or node

    def _px(n):
        try: return (n.sample("red",0,0), n.sample("green",0,0), n.sample("blue",0,0))
        except: return None

    def _vtry(name, func):
        try:
            func()
            print("[V3]   {:40s} OK".format(name))
            return True
        except Exception as e:
            print("[V3]   {:40s} ERR: {}".format(name, str(e)[:60]))
            return False

    app = QtWidgets.QApplication.instance()

    # =====================================================================
    # PART A: CONTROL TEST — Does a plain Read node update thumbnail?
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] PART A: Control Test (plain Read node)")
    print("-" * 70)

    test_read = None
    try:
        # Create temp Read node next to our node
        ox = node["xpos"].value() + 200 if "xpos" in node.knobs() else 0
        oy = node["ypos"].value() if "ypos" in node.knobs() else 0
        test_read = nuke.nodes.Read(file=image_path, xpos=ox, ypos=oy)
        test_read["postage_stamp"].setValue(True)

        px_before = _px(test_read)
        if px_before:
            print("[V3]   Created Read '{}', px={:.4f},{:.4f},{:.4f}".format(
                test_read.name(), *px_before))

            # Now change its file to a DIFFERENT image
            print("[V3]   Changing Read file...")
            test_read["file"].fromUserText(image_path)  # Same file first (ensure loaded)
            px_after = _px(test_read)
            if px_after:
                print("[V3]   After set, px={:.4f},{:.4f},{:.4f}".format(*px_after))

            # Check if DAG shows updated thumbnail for this Read
            print("[V3] >>> Look at the TEMP Read node '{}' in DAG:".format(test_read.name()))
            print("[V3]     Does it show the correct image thumbnail?")
            print("[V3]     (This tests if Nuke updates Read node thumbnails at all)")

            _time.sleep(1)
    except Exception as e:
        print("[V3]   Control test error: {}".format(e))

    # =====================================================================
    # PART B: SAFE REFRESH — never clear file, just direct set + triggers
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] PART B: Safe Refresh (NEVER clear file)")
    print("-" * 70)

    # First ensure clean state
    print("[V3] Setting file (safe, no clear)...")
    if is_group: node.begin()
    read_target["file"].fromUserText(image_path)
    if is_group: node.end()
    base_px = _px(node)
    if base_px:
        print("[V3]   Base px={:.4f},{:.4f},{:.4f}".format(*base_px))

    # B1: Simple setValue (not fromUserText) on postage_stamp
    _vtry("B1.ps_setValue_True",
          lambda: node["postage_stamp"].setValue(True))

    # B2: Sample to force compute, then toggle ps
    def _b2():
        _px(node)  # Force compute
        node["postage_stamp"].setValue(False)
        _time.sleep(0.05)
        node["postage_stamp"].setValue(True)
    _vtry("B2.sample+ps_toggle_50ms", _b2)

    # B3: Process ALL pending Qt events multiple times
    def _b3():
        for _ in range(5):
            QtWidgets.QApplication.processEvents(
                QtCore.QEventLoop.AllEvents, 50)
            _time.sleep(0.02)
    _vtry("B3.processEvents_x5", _b3)

    # B4: Touch "tile_color" knob (changes node appearance → forces redraw)
    def _b4():
        if "tile_color" in node.knobs():
            old = node["tile_color"].value()
            node["tile_color"].setValue(old)
    _vtry("B4.touch_tile_color", _b4)

    # B5: Touch "note_font" or "note_font_size"
    def _b5():
        for kn in ["note_font", "postage_stamp", "selected", "gl_renderer"]:
            if kn in node.knobs():
                try:
                    v = node[kn].value()
                    node[kn].setValue(v)
                    break
                except: pass
    _vtry("B5.touch_render_knob", _b5)

    # B6: Use begin/complete wrapped operation (atomic from Nuke's POV)
    def _b6():
        node.begin()
        rd = internal_read or node
        rd["file"].fromUserText(image_path)  # Set directly, no clear
        node["postage_stamp"].setValue(True)
        node.end()
        nuke.modified()
    _vtry("B6.atomic_begin_end_ps", _b6)

    # =====================================================================
    # PART C: EXECUTION-BASED APPROACHES
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] PART C: Execution / Render-based")
    print("-" * 70)

    # C1: Use nuke.render() or proper execute for non-Write nodes
    def _c1():
        # Try rendering just the required range
        import nuke as _nk
        f = int(_nk.frame())
        # Method: use execute on the internal Read (which is executable)
        if internal_read:
            if is_group: node.begin()
            try:
                _nk.execute(internal_read, f, f)
            except Exception as ex:
                print("[V3]     exec IR err: {}".format(ex))
            if is_group: node.end()
    _vtry("C1.exec_InternalRead_only", _c1)

    # C2: Use render with a temporary Write inside the Group
    def _c2():
        if not is_group: return
        node.begin()
        try:
            # Create temp Write connected after InternalRead, render 1 frame, delete
            tmp_write = nuke.nodes.Write(file="C:/temp/nb_tmp_####.exr",
                                         name="__tmp_nb_write__")
            tmp_write.setInput(0, internal_read)
            try:
                nuke.execute(tmp_write, 1, 1)
            except Exception as ex:
                print("[V3]     exec Write err: {}".format(ex))
            nuke.delete(tmp_write)
        except Exception as e:
            print("[V3]     setup err: {}".format(e))
        node.end()
    _vtry("C2.temp_Write_inside_Group", _c2)

    # C3: nuke.executeInMainThreadWithCallback (if available)
    def _c3():
        def _cb(result):
            print("[V3]     mainThreadCb done: {}".format(result))
        
        def _work():
            node["postage_stamp"].setValue(False)
            node["postage_stamp"].setValue(True)
            return "ps_toggled"

        try:
            nuke.executeInMainThreadWithCallback(_cb, _work)
        except AttributeError:
            # Fallback to regular executeInMainThread
            nuke.executeInMainThread(_work)
    _vtry("C3.mainThread_with_callback", _c3)

    # =====================================================================
    # PART D: HASH / DIRTY FLAG APPROACHES
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] Part D: Dirty Flag / Hash approaches")
    print("-" * 70)

    # D1: Check available internal methods
    print("[V3]   Node methods containing 'hash','dirty','valid','refresh','update':")
    interesting_methods = []
    for attr_name in dir(node):
        low = attr_name.lower()
        if any(k in low for k in ["hash", "dirty", "valid", "refresh", "update",
                                   "rebuild", "recalc", "invalidate", "render"]):
            interesting_methods.append(attr_name)
    if interesting_methods:
        print("[V3]     Found: [{}]".format(", ".join(interesting_methods)))
    else:
        print("[V3]     None found")

    # D2: Try calling any promising ones
    for m in ["forceValidate", "validate", "markDirty", "setDirty"]:
        if hasattr(node, m):
            def _call_m(method=m):
                getattr(node, method)()
            _vtry("D2.node.{}()".format(m), _call_m)

    # D3: Try Nuke's internal "update" command variants  
    def _d3():
        import nuke as _n
        nodeFullName = node.name()
        for cmd in ["idletasks", "update idletasks"]:
            try:
                _n.tcl(cmd)
                break
            except: pass
    _vtry("D3.tcl_idletasks_variants", _d3)

    # =====================================================================
    # PART E: QT EVENT INJECTION
    # =====================================================================
    print("\n" + "-" * 70)
    print("[V3] PART E: Qt Event Injection")
    print("-" * 70)

    # E1: Send mouse press+release on the node's position in DAG
    def _e1():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if not s: continue
                # Map node position to scene coords
                nx = node["xpos"].value() if "xpos" in node.knobs() else 0
                ny = node["ypos"].value() if "ypos" in node.knobs() else 0
                scene_pos = QtCore.QPointF(nx * 10, ny * 10)  # Approximate mapping
                global_pos = gv.mapToGlobal(gv.mapFromScene(scene_pos))

                # Send mouse events
                press = QtGui.QMouseEvent(
                    QtCore.QEvent.MouseButtonPress, gv.mapFromScene(scene_pos),
                    global_pos, QtCore.Qt.LeftButton,
                    QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
                release = QtGui.QMouseEvent(
                    QtCore.QEvent.MouseButtonRelease, gv.mapFromScene(scene_pos),
                    global_pos, QtCore.Qt.LeftButton,
                    QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)

                QtWidgets.QApplication.sendEvent(gv.viewport(), press)
                _time.sleep(0.02)
                QtWidgets.QApplication.sendEvent(gv.viewport(), release)
                print("[V3]     Injected click at ({}, {})".format(nx, ny))
                break
    _vtry("E1.mouse_click_on_node", _e1)

    # E2: Double-click on node (opens properties, forces refresh)
    def _e2():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if not s: continue
                nx = node["xpos"].value() if "xpos" in node.knobs() else 0
                ny = node["ypos"].value() if "ypos" in node.knobs() else 0
                scene_pos = QtCore.QPointF(nx * 10, ny * 10)
                global_pos = gv.mapToGlobal(gv.mapFromScene(scene_pos))

                dclick = QtGui.QMouseEvent(
                    QtCore.QEvent.MouseButtonDblClick, gv.mapFromScene(scene_pos),
                    global_pos, QtCore.Qt.LeftButton,
                    QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
                QtWidgets.QApplication.sendEvent(gv.viewport(), dclick)
                print("[V3]     Injected double-click")
                # Close property panel quickly
                _time.sleep(0.1)
                break
    _vtry("E2.dblclick_node", _e2)

    # E3: Focus change trick
    def _e3():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                gv.setFocus()
                gv.viewport().setFocus()
                gv.clearFocus()
                node.setSelected(True)
                break
    _vtry("E3.focus_cycle", _e3)

    # =====================================================================
    # CLEANUP & SUMMARY
    # =====================================================================
    print("\n" + "=" * 70)
    print("[V3] CLEANUP: Removing temp Read node...")
    if test_read:
        try:
            nuke.delete(test_read)
            print("[V3]   Deleted '{}'".format(test_read.name()))
        except Exception as e:
            print("[V3]   Delete err: {}".format(e))

    final_px = _px(node)
    if final_px:
        print("[V3] Final px={:.4f},{:.4f},{:.4f}".format(*final_px))

    print("\n[V3] === END === Watch DAG: Did ANYTHING update the thumbnail?")
    print("=" * 70)


def diagnose_visual_refresh_v4(node_name=None, image_path=None):
    """V4: Use Read node's built-in 'reload' knob + nuke.updateUI() + nuke.show().
    
    Key discoveries from web search:
      1. Read nodes have a 'reload' knob button: node['reload'].execute()
         This forces Nuke to re-read the file from disk AND refresh the thumbnail.
      2. nuke.updateUI() forces a full UI refresh cycle.
      3. nuke.show(node) forces node properties refresh.
    """
    import time as _time

    print("=" * 70)
    print("[V4] diagnose_visual_refresh_v4 START")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            print("[V4] ERROR: No node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[V4] ERROR: '{}' not found!".format(node_name)); return

    is_group = (node.Class() == "Group")
    internal_read = _get_internal_read_nb(node) if is_group else None
    read_target = internal_read or node

    print("[V4] Target: '{}' Class={}, InternalRead={}".format(
        node_name, node.Class(), internal_read.name() if internal_read else "None"))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs(): image_path = node["nb_file"].value()
        elif "file" in node.knobs(): image_path = node["file"].value()
    print("[V4] image_path = {}".format(repr(image_path)))

    def _px(n):
        try: return (n.sample("red",0,0), n.sample("green",0,0), n.sample("blue",0,0))
        except: return None

    px_before = _px(node)
    if px_before:
        print("[V4] BEFORE pixel = {:.4f},{:.4f},{:.4f}".format(*px_before))

    # =====================================================================
    # Step 1: Set file path on InternalRead
    # =====================================================================
    print("\n[V4] --- Step 1: Set file on InternalRead ---")
    if is_group: node.begin()
    read_target["file"].fromUserText(image_path)
    if is_group: node.end()
    print("[V4]   file set OK")

    # =====================================================================
    # Step 2: List ALL knobs on InternalRead (looking for 'reload')
    # =====================================================================
    print("\n[V4] --- Step 2: InternalRead knobs ---")
    ir_knobs = sorted(read_target.knobs().keys())
    print("[V4]   Total knobs: {}".format(len(ir_knobs)))
    # Print button-type and interesting knobs
    interesting = ["reload", "localize", "update", "refresh", "read", 
                   "postage_stamp", "file", "proxy", "cacheLocal",
                   "on_error", "format"]
    for kname in ir_knobs:
        if any(i in kname.lower() for i in interesting):
            k = read_target[kname]
            ktype = type(k).__name__
            print("[V4]   {} ({})".format(kname, ktype))

    # =====================================================================
    # Step 3: Try reload knob on InternalRead
    # =====================================================================
    print("\n[V4] --- Step 3: InternalRead['reload'].execute() ---")
    if "reload" in read_target.knobs():
        try:
            if is_group: node.begin()
            read_target["reload"].execute()
            if is_group: node.end()
            print("[V4]   reload.execute() OK!")
        except Exception as e:
            print("[V4]   reload.execute() FAIL: {}".format(e))
            if is_group:
                try: node.end()
                except: pass
    else:
        print("[V4]   NO 'reload' knob on InternalRead!")
        print("[V4]   Trying fromScript/setValue alternatives...")
        # Try knobChanged approach
        try:
            if is_group: node.begin()
            cur = read_target["file"].value()
            read_target["file"].fromUserText("")
            read_target["file"].fromUserText(cur)
            if is_group: node.end()
            print("[V4]   file clear+reload done")
        except Exception as e:
            print("[V4]   file reload FAIL: {}".format(e))

    # =====================================================================
    # Step 4: Also list Group-level knobs (looking for reload/update)
    # =====================================================================
    print("\n[V4] --- Step 4: Group-level knobs ---")
    grp_knobs = sorted(node.knobs().keys())
    for kname in grp_knobs:
        if any(i in kname.lower() for i in interesting):
            k = node[kname]
            ktype = type(k).__name__
            print("[V4]   {} ({})".format(kname, ktype))

    # =====================================================================
    # Step 5: Try nuke.updateUI()
    # =====================================================================
    print("\n[V4] --- Step 5: nuke.updateUI() ---")
    try:
        nuke.updateUI()
        print("[V4]   nuke.updateUI() OK")
    except Exception as e:
        print("[V4]   nuke.updateUI() FAIL: {}".format(e))

    # =====================================================================
    # Step 6: Try nuke.show(node)
    # =====================================================================
    print("\n[V4] --- Step 6: nuke.show(node) ---")
    try:
        nuke.show(node)
        print("[V4]   nuke.show() OK")
    except Exception as e:
        print("[V4]   nuke.show() FAIL: {}".format(e))

    # =====================================================================
    # Step 7: Toggle ps + nuke.updateUI combo
    # =====================================================================
    print("\n[V4] --- Step 7: ps toggle + updateUI combo ---")
    try:
        node["postage_stamp"].setValue(False)
        nuke.updateUI()
        QtWidgets.QApplication.processEvents()
        _time.sleep(0.1)
        node["postage_stamp"].setValue(True)
        nuke.updateUI()
        QtWidgets.QApplication.processEvents()
        print("[V4]   combo OK")
    except Exception as e:
        print("[V4]   combo FAIL: {}".format(e))

    # =====================================================================
    # Step 8: setFlag(0) approach
    # =====================================================================
    print("\n[V4] --- Step 8: postage_stamp.setFlag(0) ---")
    try:
        node["postage_stamp"].setFlag(0)
        print("[V4]   setFlag(0) OK")
    except Exception as e:
        print("[V4]   setFlag(0) FAIL: {}".format(e))

    # =====================================================================
    # Step 9: Root node force refresh
    # =====================================================================
    print("\n[V4] --- Step 9: Root setModified + frame jog ---")
    try:
        nuke.root().setModified(True)
        cur_f = nuke.frame()
        nuke.frame(cur_f)  # Jump to same frame, triggering re-evaluate
        print("[V4]   root.setModified + frame() OK")
    except Exception as e:
        print("[V4]   root refresh FAIL: {}".format(e))

    # =====================================================================
    # Step 10: CONTROL — Create plain Read node + see if it shows thumbnail
    # =====================================================================
    print("\n[V4] --- Step 10: Control — Create plain Read + check ---")
    test_read = None
    try:
        test_read = nuke.nodes.Read(file=image_path)
        test_read["postage_stamp"].setValue(True)
        nuke.updateUI()
        QtWidgets.QApplication.processEvents()
        _time.sleep(0.5)
        
        rpx = _px(test_read)
        print("[V4]   Created '{}', px={}".format(
            test_read.name(),
            "{:.4f},{:.4f},{:.4f}".format(*rpx) if rpx else "N/A"))
        print("[V4]   Does this Read node show a thumbnail in DAG?")
        print("[V4]   (If NOT, then postage_stamp is globally disabled in Preferences!)")
        
        # Try reload on this Read too
        if "reload" in test_read.knobs():
            test_read["reload"].execute()
            print("[V4]   Read reload.execute() OK")
    except Exception as e:
        print("[V4]   Control test FAIL: {}".format(e))

    # =====================================================================
    # Step 11: Check global postage_stamp preferences
    # =====================================================================
    print("\n[V4] --- Step 11: Check Nuke Preferences for postage stamps ---")
    try:
        root = nuke.root()
        prefs = nuke.toNode("preferences")
        if prefs:
            pref_knobs = sorted(prefs.knobs().keys())
            ps_prefs = [k for k in pref_knobs if "postage" in k.lower() or "stamp" in k.lower()]
            print("[V4]   Postage-stamp related prefs: {}".format(ps_prefs))
            for pk in ps_prefs:
                print("[V4]     {} = {}".format(pk, prefs[pk].value()))
        else:
            print("[V4]   'preferences' node not found")
    except Exception as e:
        print("[V4]   Prefs check FAIL: {}".format(e))

    px_after = _px(node)
    if px_after:
        print("\n[V4] AFTER pixel = {:.4f},{:.4f},{:.4f} (changed={})".format(
            *px_after, px_before != px_after))

    print("\n[V4] >>> CRITICAL QUESTION: Does the CONTROL Read8 show a thumbnail?")
    print("[V4]     If NO → Nuke postage_stamp is globally OFF / broken")
    print("[V4]     If YES → Group nodes need different treatment")
    print("=" * 70)


def diagnose_visual_refresh_v5(node_name=None, image_path=None):
    """V5 Diagnostic: 'Replacement Jutsu' — delete + rebuild the Group node.

    After 30+ failed refresh techniques (V1-V4), this approach sidesteps the
    stale-cache problem entirely by creating a brand-new C++ node instance
    via nuke.nodeCopy / nuke.nodePaste.

    Call from Nuke Script Editor:
        import importlib, ai_workflow.nb_diagnostics as nbd
        importlib.reload(nbd)
        nbd.diagnose_visual_refresh_v5("Nano_Viewer9",
            "E:/BaiduNetdiskDownload/nuke_workflow/temp/NanoBanana_Generate_frame5.jpg")
    """
    import time as _time

    print("=" * 70)
    print("[V5] diagnose_visual_refresh_v5 START  (Replacement Jutsu)")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            print("[V5] ERROR: No NB Player node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[V5] ERROR: '{}' not found!".format(node_name)); return

    print("[V5] Target: '{}' (Class={})".format(node_name, node.Class()))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs():
            image_path = node["nb_file"].value()
        elif "file" in node.knobs():
            image_path = node["file"].value()
    print("[V5] image_path = {}".format(repr(image_path)))

    if image_path and not os.path.isfile(image_path):
        print("[V5] WARNING: File missing: {}".format(repr(image_path)))

    # --- Sample BEFORE ---
    def _px(n):
        try: return (n.sample("red",0,0), n.sample("green",0,0), n.sample("blue",0,0))
        except: return None

    px_before = _px(node)
    if px_before:
        print("[V5] BEFORE pixel = {:.4f},{:.4f},{:.4f}".format(*px_before))

    # --- Record old state for comparison ---
    old_xpos = int(node["xpos"].value())
    old_ypos = int(node["ypos"].value())
    old_inputs = []
    for i in range(node.inputs()):
        inp = node.input(i)
        old_inputs.append(inp.name() if inp else None)
    old_deps = []
    for dep in node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS):
        for i in range(dep.inputs()):
            if dep.input(i) == node:
                old_deps.append((dep.name(), i))

    print("[V5] Old pos: ({}, {})".format(old_xpos, old_ypos))
    print("[V5] Old inputs: {}".format(old_inputs))
    print("[V5] Old downstream: {}".format(old_deps))

    # =====================================================================
    # THE REPLACEMENT JUTSU
    # =====================================================================
    print("\n[V5] --- Executing Replacement Jutsu ---")
    new_node = _rebuild_group_for_thumbnail(node, image_path)

    if not new_node:
        print("[V5] FAILED! _rebuild_group_for_thumbnail returned None")
        print("[V5] Falling back to legacy _update_node_thumbnail...")
        # The old node may be deleted; try to find by name
        fallback = nuke.toNode(node_name)
        if fallback:
            _update_node_thumbnail(fallback, image_path)
        print("=" * 70)
        return

    # =====================================================================
    # Verify the new node
    # =====================================================================
    print("\n[V5] --- Verification ---")
    print("[V5] New node: '{}' (Class={})".format(new_node.name(), new_node.Class()))
    print("[V5] New pos: ({}, {})".format(
        int(new_node["xpos"].value()), int(new_node["ypos"].value())))

    # Check connections restored
    new_inputs = []
    for i in range(new_node.inputs()):
        inp = new_node.input(i)
        new_inputs.append(inp.name() if inp else None)
    print("[V5] New inputs: {}".format(new_inputs))

    new_deps = []
    for dep in new_node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS):
        for i in range(dep.inputs()):
            if dep.input(i) == new_node:
                new_deps.append((dep.name(), i))
    print("[V5] New downstream: {}".format(new_deps))

    # Check InternalRead has correct file
    ir = _get_internal_read_nb(new_node)
    if ir:
        ir_file = ir["file"].value()
        print("[V5] InternalRead file: {}".format(repr(ir_file)))
    else:
        print("[V5] WARNING: No InternalRead in new node!")

    # Check pixel data
    px_after = _px(new_node)
    if px_after:
        print("[V5] AFTER pixel = {:.4f},{:.4f},{:.4f}".format(*px_after))

    # Check postage_stamp
    if "postage_stamp" in new_node.knobs():
        print("[V5] postage_stamp = {}".format(new_node["postage_stamp"].value()))

    # Check nb_file
    if "nb_file" in new_node.knobs():
        print("[V5] nb_file = {}".format(repr(new_node["nb_file"].value())))

    print("\n[V5] >>> CHECK THE DAG NOW!")
    print("[V5]     Does '{}' show the CORRECT thumbnail?".format(new_node.name()))
    print("[V5]     (The node was deleted and recreated — a fresh C++ instance)")
    print("=" * 70)

    return new_node


def diagnose_visual_refresh(node_name=None, image_path=None):
    """V2 Diagnostic: Focus on DAG VISUAL refresh only.
    
    Data layer is CONFIRMED working (Group pixels update correctly after file change).
    The remaining problem: DAG view shows STALE thumbnail despite correct pixels.
    
    Tests visual-only refresh techniques on the QGraphicsView / NodeItem level.
    """
    print("=" * 70)
    print("[VIS] diagnose_visual_refresh START")
    print("=" * 70)

    # --- Find target ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd; break
        if not found:
            for nd in nuke.allNodes("Read"): found = nd; break
        if not found:
            print("[VIS] ERROR: No node found!"); return
        node_name = found.name()

    node = nuke.toNode(node_name)
    if not node:
        print("[VIS] ERROR: '{}' not found!".format(node_name)); return

    is_group = (node.Class() == "Group")
    print("[VIS] Target: '{}' (Class={})".format(node_name, node.Class()))

    # --- Get image_path ---
    if not image_path:
        if "nb_file" in node.knobs(): image_path = node["nb_file"].value()
        elif "file" in node.knobs(): image_path = node["file"].value()
    if not os.path.isfile(image_path):
        print("[VIS] WARNING: File missing: {}".format(repr(image_path)))

    internal_read = _get_internal_read_nb(node) if is_group else None
    read_target = internal_read or node

    def _safe_sample(n):
        try: return (n.sample("red",0,0), n.sample("green",0,0), n.sample("blue",0,0))
        except: return None

    # =====================================================================
    # PHASE 0: Restore to known-good state
    # =====================================================================
    print("\n[VIS] PHASE 0: Restoring file...")
    try:
        if is_group: node.begin()
        read_target["file"].fromUserText(image_path)
        if is_group: node.end()
        px0 = _safe_sample(node)
        if px0: print("[VIS]   pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*px0))
    except Exception as e:
        print("[VIS]   FAIL: {}".format(e)); return

    # Ensure ps is ON
    if "postage_stamp" in node.knobs():
        node["postage_stamp"].setValue(True)

    # =====================================================================
    # PHASE 1: Enumerate all DAG-related widgets in detail
    # =====================================================================
    print("\n[VIS] PHASE 1: Enumerating DAG widgets...")
    app = QtWidgets.QApplication.instance()
    dag_info = {"qgv": [], "scene": [], "viewport": []}

    if app:
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                cn = gv.metaObject().className()
                gv_id = "{}@{}".format(cn, id(gv))
                dag_info["qgv"].append(gv_id)

                scene = gv.scene()
                if scene:
                    n_items = len(scene.items())
                    dag_info["scene"].append("{} has {} items".format(gv_id, n_items))

                    # Look for items containing our node name
                    for item in scene.items():
                        try:
                            # Try to get item text/data
                            item_data = str(type(item).__name__)
                            if hasattr(item, 'toolTip'):
                                tt = item.toolTip()
                                if node_name in str(tt):
                                    dag_info["viewport"].append(
                                        "FOUND NodeItem: {} tooltip={}".format(item_data, tt[:60]))
                        except:
                            pass

                vp = gv.viewport()
                if vp:
                    dag_info["viewport"].append("viewport={}@{}".format(
                        type(vp).__name__, id(vp)))

    for cat, items in dag_info.items():
        print("[VIS]   {}: [{}]".format(cat, "; ".join(items) if items else "NONE"))

    # =====================================================================
    # PHASE 2: Visual refresh techniques (each followed by user check)
    # =====================================================================
    print("\n[VIS] PHASE 2: Testing VISUAL refresh techniques...")
    print("[VIS] Watch the DAG view after each technique!\n")

    results = []

    def _vtry(name, func):
        """Try one visual refresh technique."""
        try:
            func()
            results.append((name, "OK"))
            print("[VIS]   {:35s} OK".format(name))
        except Exception as e:
            results.append((name, "ERR:{}".format(str(e)[:40])))
            print("[VIS]   {:35s} ERR: {}".format(name, str(e)[:50]))

    # --- Technique 1: Full QGraphicsScene invalidate + repaint ---
    def _t1_full_invalidate():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if s:
                    # Invalidate ALL layers
                    s.invalidate(s.sceneRect(), QtWidgets.QGraphicsScene.AllLayers)
                    s.update(s.sceneRect())
                gv.viewport().repaint()
                gv.repaint()
                gv.viewport().update()
    _vtry("T1.full_scene_invalidate+repaint", _t1_full_invalidate)

    # --- Technique 2: Toggle postage_stamp off→on (classic approach) ---
    def _t2_toggle_ps():
        node["postage_stamp"].setValue(False)
        node.processEvents() if hasattr(node, "processEvents") else None
        import time; time.sleep(0.05)
        node["postage_stamp"].setValue(True)
    _vtry("T2.toggle_ps_with_delay", _t2_toggle_ps)

    # --- Technique 3: Select/deselect to trigger node redraw ---
    def _t3_select_cycle():
        was_sel = node.isSelected()
        node.setSelected(False)
        QtWidgets.QApplication.processEvents()
        node.setSelected(True)
        QtWidgets.QApplication.processEvents()
        node.setSelected(was_sel)
    _vtry("T3.select_deselect_cycle", _t3_select_cycle)

    # --- Technique 4: Force DAG viewport full redraw via painter ---
    def _t4_viewport_redraw():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                vp = gv.viewport()
                # Force paint event
                p = QtGui.QPainter(vp)
                p.end()
                vp.update(vp.rect())
                # Also trigger resize event (forces full redraw)
                geo = gv.geometry()
                gv.setGeometry(geo.x(), geo.y(), geo.width()+1, geo.height())
                gv.setGeometry(geo)
    _vtry("T4.viewport_paint+resize_trick", _t4_viewport_redraw)

    # --- Technique 5: nuke.modified() + nuke.tcl(idletasks) ---
    def _t5_nuke_refresh():
        nuke.modified()
        __import__("nuke").tcl("idletasks")
        __import__("nuke").tcl("update idletasks")
        QtWidgets.QApplication.processEvents()
        QtWidgets.QApplication.sendPostedEvents(None, 0)
    _vtry("T5.nuke_modified+idletasks", _t5_nuke_refresh)

    # --- Technique 6: Find NodeItem in scene and call update() directly ---
    def _t6_nodeitem_update():
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if s:
                    updated = 0
                    for item in s.items():
                        item_str = str(type(item))
                        # NodeItem classes in Nuke DAG
                        if any(k in item_str for k in ["Node", "Item"]):
                            item.update()
                            updated += 1
                    if updated > 0:
                        print("[VIS]     Updated {} items in scene".format(updated))
    _vtry("T6.nodeitem_direct_update", _t6_nodeitem_update)

    # --- Technique 7: Change node position slightly (forces DAG relayout) ---
    def _t7_nudge_position():
        if "xpos" in node.knobs() and "ypos" in node.knobs():
            old_x = node["xpos"].value()
            old_y = node["ypos"].value()
            node["xpos"].setValue(old_x)
            node["ypos"].setValue(old_y)
    _vtry("T7.nudge_xpos_ypos_knob", _t7_nudge_position)

    # --- Technique 8: Re-read file + ps toggle in main thread ---
    def _t8_mainthread_reread():
        def _do_it():
            if is_group: node.begin()
            rd = internal_read or node
            cur = rd["file"].value()
            rd["file"].fromUserText("")
            rd["file"].fromUserText(cur)
            if is_group: node.end()
            node["postage_stamp"].setValue(False)
            node["postage_stamp"].setValue(True)
        nuke.executeInMainThread(_do_it)
    _vtry("T8.mainThread_reread+ps", _t8_mainthread_reread)

    # --- Technique 9: QTimer delayed cascade (ps toggle → invalidate → select) ---
    def _t9_delayed_cascade():
        _n = node
        def _step1():
            try:
                _n["postage_stamp"].setValue(False)
                _n["postage_stamp"].setValue(True)
            except: pass
        def _step2():
            for tw in app.topLevelWidgets():
                for gv in tw.findChildren(QtWidgets.QGraphicsView):
                    s = gv.scene()
                    if s: s.invalidate(s.sceneRect(), QtWidgets.QGraphicsScene.AllLayers)
        def _step3():
            _n.setSelected(False)
            _n.setSelected(True)
        QtCore.QTimer.singleShot(100, _step1)
        QtCore.QTimer.singleShot(300, _step2)
        QtCore.QTimer.singleShot(500, _step3)
    _vtry("T9.cascade_100_300_500ms", _t9_delayed_cascade)

    # --- Technique 10: Touch knob_value_changed callback trigger ---
    def _t10_touch_label():
        if "label" in node.knobs():
            old = node["label"].value()
            node["label"].setValue(old)
    _vtry("T10.touch_label_knob", _t10_touch_label)

    # --- Technique 11: Hide then show node from DAG ---
    def _t11_hide_show():
        if "hide_input" in node.knobs():
            node["hide_input"].setValue(True)
            QtWidgets.QApplication.processEvents()
            node["hide_input"].setValue(False)
        else:
            # Use opacity knob if available
            if "opacity" in node.knobs():
                node["opacity"].setValue(0.0)
                QtWidgets.QApplication.processEvents()
                node["opacity"].setValue(1.0)
    _vtry("T11.hide_show_toggle", _t11_hide_show)

    # =====================================================================
    # Summary
    # =====================================================================
    print("\n" + "=" * 70)
    print("[VIS] SUMMARY:")
    ok_count = sum(1 for _, s in results if s == "OK")
    err_count = len(results) - ok_count
    print("[VIS]   OK: {}  |  ERR: {}".format(ok_count, err_count))
    for name, status in results:
        print("[VIS]   {:35s} {}".format(name, status))

    final_px = _safe_sample(node)
    if final_px:
        print("\n[VIS] Final pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*final_px))
    
    print("\n[VIS] >>> Check DAG NOW: Did ANY technique update the thumbnail? <<<")
    print("=" * 70)


def test_thumbnail_refresh(node_name=None, image_path=None):
    """Test function: update a Nano Viewer thumbnail and try all refresh methods.

    Call from Nuke's Python Console:
        # Example 1: Use existing Nano_Viewer7 with a new image
        import ai_workflow.nb_diagnostics as nbd
        nbd.test_thumbnail_refresh("Nano_Viewer7", "E:/path/to/new_image.jpg")

        # Example 2: Auto-find first NB Player node and refresh current image
        nbd.test_thumbnail_refresh()

        # Example 3: Test on a Read node
        n = nuke.nodes.Read(file="E:/path/to/image.jpg")
        nbd.test_thumbnail_refresh(n.name())
    """

    print("=" * 60)
    print("[TEST] test_thumbnail_refresh START")
    print("[TEST]   node_name = {}".format(repr(node_name)))
    print("[TEST]   image_path = {}".format(repr(image_path)))

    # --- Find the target node ---
    if not node_name:
        found = None
        for nd in nuke.allNodes("Group"):
            if "is_nb_player" in nd.knobs() and nd["is_nb_player"].value():
                found = nd
                break
        if not found:
            reads = [n for n in nuke.allNodes("Read")]
            if reads:
                found = reads[0]
        if not found:
            print("[TEST] ERROR: No NB Player or Read node found!")
            return
        node_name = found.name()
        print("[TEST]   Auto-selected: '{}'".format(node_name))

    node = nuke.toNode(node_name)
    if not node:
        print("[TEST] ERROR: Node '{}' not found!".format(node_name))
        return

    print("[TEST]   Node '{}' (Class={})".format(node.name(), node.Class()))

    # --- Determine image path ---
    if not image_path:
        if "nb_file" in node.knobs():
            image_path = node["nb_file"].value()
        elif "file" in node.knobs():
            image_path = node["file"].value()
        else:
            print("[TEST] ERROR: No file knob!")
            return
        if not os.path.isfile(image_path):
            print("[TEST] WARNING: File doesn't exist: {}".format(repr(image_path)))

    internal_read = _get_internal_read_nb(node) if node.Class() == "Group" else None

    # --- Sample BEFORE change ---
    old_px = (0, 0, 0)
    try:
        old_px = (node.sample("red", 0, 0), node.sample("green", 0, 0), node.sample("blue", 0, 0))
    except:
        pass
    print("[TEST] BEFORE pixel(0,0) = {:.4f},{:.4f},{:.4f}".format(*old_px))

    # --- Update file path ---
    print("\n[TEST] Updating to: {}".format(image_path))
    if internal_read:
        node.begin()
        internal_read["file"].fromUserText(image_path)
        node.end()
    elif "file" in node.knobs():
        node["file"].fromUserText(image_path)

    # --- Sample AFTER change ---
    new_px = (0, 0, 0)
    try:
        new_px = (node.sample("red", 0, 0), node.sample("green", 0, 0), node.sample("blue", 0, 0))
    except:
        pass
    print("[TEST] AFTER  pixel(0,0) = {:.4f},{:.4f},{:.4f} (changed={})".format(
        *new_px, old_px != new_px))

    # Ensure postage_stamp is ON
    if "postage_stamp" in node.knobs():
        node["postage_stamp"].setValue(True)

    # --- Try each refresh method individually ---
    results = {}

    # A: toggle postage_stamp
    try:
        node["postage_stamp"].setValue(False); node["postage_stamp"].setValue(True)
        results["A.toggle_ps"] = "OK"
    except Exception as e: results["A.toggle_ps"] = str(e)[:40]

    # B: sample force
    try:
        node.sample("red", 0, 0)
        results["B.sample"] = "OK"
    except Exception as e: results["B.sample"] = str(e)[:40]

    # C: nuke.modified
    try:
        nuke.modified(); results["C.modified"] = "OK"
    except Exception as e: results["C.modified"] = str(e)[:40]

    # D: QGraphicsView scene invalidate
    try:
        app = QtWidgets.QApplication.instance(); n_gv = 0
        for tw in app.topLevelWidgets():
            for gv in tw.findChildren(QtWidgets.QGraphicsView):
                s = gv.scene()
                if s: s.invalidate(s.sceneRect(), QtWidgets.QGraphicsScene.AllLayers)
                vp = gv.viewport()
                if vp: vp.repaint()
                n_gv += 1
        results["D.QGView({}v)".format(n_gv)] = "OK"
    except Exception as e: results["D.QGView"] = str(e)[:40]

    # E: select toggle
    try:
        s = node.isSelected(); node.setSelected(not s); node.setSelected(s)
        results["E.select_toggle"] = "OK"
    except Exception as e: results["E.select_toggle"] = str(e)[:40]

    # F: re-read file (clear + reload)
    try:
        rd = internal_read or node
        cur = rd["file"].value() if "file" in rd.knobs() else ""
        rd["file"].fromUserText(""); rd["file"].fromUserText(cur)
        results["F.reread"] = "OK"
    except Exception as e: results["F.reread"] = str(e)[:40]

    # G: delayed re-read + ps toggle at 800ms
    _nr = node; _ip = image_path; _ir = _get_internal_read_nb(_nr) if _nr.Class()=="Group" else None
    def _delayed_G():
        try:
            if _nr.Class()=="Group": _nr.begin()
            rd = _ir or _nr
            c = rd["file"].value() if "file" in rd.knobs() else ""
            rd["file"].fromUserText(""); rd["file"].fromUserText(c)
            _nr["postage_stamp"].setValue(False); _nr["postage_stamp"].setValue(True)
            if _nr.Class()=="Group": _nr.end()
            print("[TEST] Method G (800ms): done")
        except Exception as eg: print("[TEST] Method G fail: {}".format(eg))
    QtCore.QTimer.singleShot(800, _delayed_G)
    results["G.delayed_800ms"] = "SCHEDULED"

    # Print summary table
    print("\n[TEST] === REFRESH RESULTS ===")
    for m, r in sorted(results.items()):
        print("  {:20s} : {}".format(m, r))
    print("\n[TEST] Check DAG view NOW - did thumbnail update?")
    print("[TEST] Wait ~1s for Method G (delayed)")
    print("=" * 60)


def restore_nb_thumbnails():
    """Restore postage-stamp previews for all NB Player nodes in the current script.

    Called on script load so that existing NB Player nodes display their
    thumbnail in the Node Graph (like Read nodes).
    Uses Replacement Jutsu to force fresh thumbnail rendering.
    """
    restored = 0
    # Collect nodes first to avoid modifying allNodes() during iteration
    nb_players = [n for n in nuke.allNodes("Group")
                  if "is_nb_player" in n.knobs() and n["is_nb_player"].value()]
    for node in nb_players:
        img = None
        if "nb_file" in node.knobs():
            img = node["nb_file"].value()
        rebuilt = _rebuild_group_for_thumbnail(node, img)
        if not rebuilt:
            # Fallback to legacy (best-effort)
            _update_node_thumbnail(node, img)
        restored += 1
    if restored:
        print("[NB] restore: rebuilt {} node(s) for fresh thumbnails".format(restored))
