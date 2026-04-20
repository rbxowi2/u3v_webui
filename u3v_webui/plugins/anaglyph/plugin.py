"""plugins/anaglyph/plugin.py — Metadata for AnaglyphStereo (2.1.0)."""

from .basic import AnaglyphPlugin

PLUGIN_CLASS       = AnaglyphPlugin
PLUGIN_NAME        = "Anaglyph"
PLUGIN_VERSION     = "2.1.0"
PLUGIN_DESCRIPTION = "Red-cyan / red-blue anaglyph stereo from two cameras"
PLUGIN_MODE        = "pipeline"
