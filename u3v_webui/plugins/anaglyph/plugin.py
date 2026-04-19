"""plugins/anaglyph/plugin.py — Metadata for AnaglyphStereo (1.0.0)."""

from .basic import AnaglyphPlugin

PLUGIN_CLASS       = AnaglyphPlugin
PLUGIN_NAME        = "Anaglyph"
PLUGIN_TYPE        = "global"
PLUGIN_VERSION     = "1.0.0"
PLUGIN_DESCRIPTION = "Red-blue anaglyph stereo from two cameras"
PLUGIN_MODE        = "display"   # always reads the final pipeline result
