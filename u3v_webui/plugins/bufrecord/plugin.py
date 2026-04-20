"""plugins/bufrecord/plugin.py — Metadata export for BasicBufRecord (1.0.0)."""

from .basic import BasicBufRecord

PLUGIN_CLASS       = BasicBufRecord
PLUGIN_NAME        = "BasicBufRecord"
PLUGIN_VERSION     = "1.0.0"
PLUGIN_DESCRIPTION = "RAM-buffer recording (accumulate then save)"
