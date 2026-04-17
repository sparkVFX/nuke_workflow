"""
Nuke rendering utilities — render input nodes to files silently.
"""

import os
import nuke


def render_input_to_file_silent(input_node, output_path, frame=None):
    """Render a node's output to a file silently (without creating visible Write node)."""
    if frame is None:
        frame = nuke.frame()

    if input_node is None:
        return False

    write = None
    try:
        write = nuke.nodes.Write()
        write.setInput(0, input_node)
        write["file"].setValue(output_path.replace("\\", "/"))
        write["file_type"].setValue("png")
        write["channels"].setValue("rgb")

        # Hide from DAG
        write["xpos"].setValue(-10000)
        write["ypos"].setValue(-10000)

        # Execute the write silently
        nuke.execute(write, frame, frame)

        # Delete the temporary Write node immediately
        nuke.delete(write)

        return os.path.exists(output_path)
    except Exception as e:
        print("[AI Workflow] Error rendering: {}".format(str(e)))
        try:
            if write:
                nuke.delete(write)
        except Exception:
            pass
        return False


def collect_input_images(node, temp_dir):
    """Collect connected input images from a node.

    DAG layout:       img1(LEFT) ... imgN(RIGHT)
    Input index map:  imgK -> input (num_inputs - K)

    Returns list of dicts with keys: index, name, path, connected, node_name.
    """
    inputs_info = []
    render_frame = nuke.frame()
    gen_name = node.name()

    num_inputs = node.inputs()

    print("[AI Workflow] collect: '{}' has {} inputs".format(gen_name, num_inputs))

    for k in range(1, num_inputs + 1):
        input_idx = num_inputs - k
        input_node = node.input(input_idx)
        input_name = "img{}".format(k)

        info = {
            "index": k - 1,
            "name": input_name,
            "path": None,
            "connected": input_node is not None,
            "node_name": input_node.name() if input_node else None
        }

        if input_node is not None:
            filename = "{}_input_{}_frame{}.png".format(gen_name, input_name, k)
            output_path = os.path.join(temp_dir, filename)

            if render_input_to_file_silent(input_node, output_path, render_frame):
                info["path"] = output_path

        inputs_info.append(info)

    return inputs_info


def collect_input_image_paths(node):
    """Collect file paths from connected inputs (renders if needed).

    Returns list of file path strings.
    """
    from ai_workflow.core.directories import get_input_directory

    paths = []
    input_dir = get_input_directory()
    render_frame = nuke.frame()
    gen_name = node.name()
    num_inputs = node.inputs()

    for k in range(1, num_inputs + 1):
        input_idx = num_inputs - k
        input_node = node.input(input_idx)

        if input_node is not None:
            filename = "{}_img{}_frame{}.png".format(gen_name, k, int(render_frame))
            output_path = os.path.join(input_dir, filename)
            if render_input_to_file_silent(input_node, output_path, render_frame):
                paths.append(output_path)

    return paths
