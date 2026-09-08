DOMAIN = "escpos_printer"

# Configuration keys
CONF_CUPS_SERVER = "cups_server"
CONF_PRINTER_NAME = "printer_name"
CONF_TIMEOUT = "timeout"
CONF_CODEPAGE = "codepage"
CONF_DEFAULT_ALIGN = "default_align"
CONF_DEFAULT_CUT = "default_cut"
CONF_STATUS_INTERVAL = "status_interval"
CONF_PROFILE = "profile"
CONF_LINE_WIDTH = "line_width"

CONF_WIDTH_PIXELS = "width_pixels"  # per-entry image width override (dots)
CONF_IMPL = "impl"  # per-entry default image implementation

# Default values
DEFAULT_TIMEOUT = 4.0
DEFAULT_ALIGN = "left"
DEFAULT_CUT = "none"
DEFAULT_LINE_WIDTH = 48

# Image implementation selection. "auto" follows the printer profile
# (capabilities.pick_impl); the rest map straight to python-escpos's
# image(impl=...) argument.
IMPL_AUTO = "auto"
IMPL_MODES = ("bitImageRaster", "bitImageColumn", "graphics")
IMPL_CHOICE_LABELS = {
    IMPL_AUTO: "Auto (recommended) — follow the printer profile",
    "bitImageRaster": "Raster (GS v 0) — widest compatibility",
    "bitImageColumn": "Column (ESC *) — for printers without raster support",
    "graphics": "Graphics (GS ( L) — newer Epson printers",
}

# Profile selection constants (also defined in capabilities.py, imported here for convenience)
PROFILE_AUTO = ""  # Auto-detect (default) profile
PROFILE_CUSTOM = "__custom__"  # Custom profile option
OPTION_CUSTOM = "__custom__"  # Custom option for codepage/line_width dropdowns

SERVICE_PRINT_TEXT = "print_text"
SERVICE_PRINT_TEXT_UTF8 = "print_text_utf8"
SERVICE_PRINT_QR = "print_qr"
SERVICE_PRINT_IMAGE = "print_image"
SERVICE_FEED = "feed"
SERVICE_CUT = "cut"
SERVICE_PRINT_BARCODE = "print_barcode"
SERVICE_BEEP = "beep"

ATTR_TEXT = "text"
ATTR_ALIGN = "align"
ATTR_BOLD = "bold"
ATTR_UNDERLINE = "underline"
ATTR_WIDTH = "width"
ATTR_HEIGHT = "height"
ATTR_ENCODING = "encoding"
ATTR_CUT = "cut"
ATTR_FEED = "feed"
ATTR_DATA = "data"
ATTR_SIZE = "size"
ATTR_EC = "ec"
ATTR_IMAGE = "image"
ATTR_HIGH_DENSITY = "high_density"
ATTR_IMPL = "impl"
ATTR_LINES = "lines"
ATTR_MODE = "mode"

# Barcode-related
ATTR_CODE = "code"
ATTR_BC = "bc"
ATTR_BARCODE_HEIGHT = "height"
ATTR_BARCODE_WIDTH = "width"
ATTR_POS = "pos"
ATTR_FONT = "font"
ATTR_ALIGN_CT = "align_ct"
ATTR_CHECK = "check"
ATTR_FORCE_SOFTWARE = "force_software"

# Beep-related
ATTR_TIMES = "times"
ATTR_DURATION = "duration"
