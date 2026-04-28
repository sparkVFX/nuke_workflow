# AI Workflow Plugin for Nuke
# This package provides AI-powered image and video generation tools

__version__ = "1.0.0"

# Import core subpackage for convenient access
from ai_workflow import core  # noqa: F401

# ---------------------------------------------------------------------------
# Eager-import submodules that are referenced by Nuke node PyCustom_Knob
# command strings (e.g. "ai_workflow.seedance.SeedanceKnobWidget()").
#
# When Nuke opens a .nk that contains one of these nodes, it `exec`s the
# command string. Python resolves `ai_workflow.seedance` by attribute lookup
# on the `ai_workflow` package object -- if nobody has imported the submodule
# yet, the attribute is missing and you get:
#     AttributeError: module 'ai_workflow' has no attribute 'seedance'
# Pre-importing them here guarantees the attributes exist as soon as the
# package is loaded.
# ---------------------------------------------------------------------------
def _preload_node_submodules():
    import importlib
    for _name in (
        "ai_workflow.nanobanana",
        "ai_workflow.veo",
        "ai_workflow.seedance",
    ):
        try:
            importlib.import_module(_name)
        except Exception as _e:
            # Never block package import on one bad submodule -- surface the
            # failure so it can be diagnosed without breaking the rest.
            print("[ai_workflow] preload failed for {}: {}".format(_name, _e))


_preload_node_submodules()
