"""
plugins/params/plugin.py — Metadata export for BasicParamsPlugin (6.0.0).

PluginManager reads PLUGIN_CLASS, PLUGIN_NAME, PLUGIN_TYPE (and optionals)
from this module without instantiating anything.
"""

from .basic import BasicParamsPlugin

PLUGIN_CLASS       = BasicParamsPlugin
PLUGIN_NAME        = "BasicParams"
PLUGIN_TYPE        = "local"
PLUGIN_VERSION     = "2.0.0"
PLUGIN_DESCRIPTION = "Exposure, gain, FPS, auto modes"
