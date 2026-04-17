"""Helper script to create veo_nodes.py and modify veo.py."""
import os

VEO_PATH = r'e:\BaiduNetdiskDownload\nuke_workflow\ai_workflow\veo.py'
OUT_PATH = r'e:\BaiduNetdiskDownload\nuke_workflow\ai_workflow\veo_nodes.py'

with open(VEO_PATH, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Line ranges to extract (1-indexed, inclusive)
extract_ranges = [
    (525, 564),   # _SEND_TO_STUDIO_SCRIPT + _add_send_to_studio_knob
    (572, 605),   # _VEO_PLAYER_SEND_SCRIPT
    (608, 705),   # create_veo_player_node + _get_internal_read
    (708, 939),   # _rebuild_veo_group_for_thumbnail + _update_veo_thumbnail
    (945, 955),   # _next_veo_viewer_name
    (958, 1369),  # create_veo_viewer_node + create_veo_viewer_standalone + update_veo_viewer_read
    (2274, 2393), # _find_veo_generator + _collect_veo_input_images_for_round + _collect_veo_input_image_paths
    (3054, 3226), # _next_veo_name + _create_veo_group_inputs + create_veo_node
]

# Extract the code
extracted = []
for s, e in extract_ranges:
    extracted.extend(lines[s-1:e])  # Convert to 0-indexed

# Build veo_nodes.py header
header = '''"""VEO node creation and manipulation functions.

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

'''

# Write the file
with open(OUT_PATH, 'w', encoding='utf-8') as f:
    f.write(header)
    f.writelines(extracted)

# Count lines
with open(OUT_PATH, 'r', encoding='utf-8') as f:
    count = sum(1 for _ in f)
print(f'veo_nodes.py created: {count} lines')

# Now modify veo.py: delete extracted lines and add re-imports
delete_set = set()
for s, e in extract_ranges:
    for i in range(s-1, e):
        delete_set.add(i)

new_lines = []
for i, line in enumerate(lines):
    if i in delete_set:
        continue
    new_lines.append(line)

# Find the insert point (after existing imports, before the first def/class)
# Look for the last 'from ai_workflow' import line
insert_idx = None
for i, line in enumerate(new_lines):
    if line.strip().startswith('from ai_workflow.core.worker_base import'):
        insert_idx = i + 1
        break

if insert_idx is None:
    print('ERROR: Could not find insert marker')
else:
    reimport_block = '''
# Backward-compatible re-exports from veo_nodes
# (node creation functions extracted for maintainability)
from ai_workflow.veo_nodes import (  # noqa: F401
    _SEND_TO_STUDIO_SCRIPT,
    _VEO_PLAYER_SEND_SCRIPT,
    _add_send_to_studio_knob,
    create_veo_player_node,
    _get_internal_read,
    _rebuild_veo_group_for_thumbnail,
    _update_veo_thumbnail,
    _next_veo_viewer_name,
    create_veo_viewer_node,
    create_veo_viewer_standalone,
    update_veo_viewer_read,
    _find_veo_generator,
    _collect_veo_input_images_for_round,
    _collect_veo_input_image_paths,
    _next_veo_name,
    _create_veo_group_inputs,
    create_veo_node,
)
'''
    new_lines.insert(insert_idx, reimport_block)

with open(VEO_PATH, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print(f'veo.py: {len(lines)} -> {len(new_lines)} lines (removed {len(lines) - len(new_lines)})')
