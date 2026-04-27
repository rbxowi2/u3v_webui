from .calibrate import LensCalibrate

PLUGIN_CLASS       = LensCalibrate
PLUGIN_NAME        = "LensCalibrate"
PLUGIN_VERSION     = "2.2.0"   # Two-stage fisheye calibration; remove K₀ image-circle section
PLUGIN_DESCRIPTION = "Lens distortion calibration — normal (pinhole) and fisheye"
PLUGIN_MODE        = "pipeline"
