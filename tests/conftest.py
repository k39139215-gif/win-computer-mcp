"""
conftest.py - Pytest configuration ensuring interactive desktop attachment.
"""

import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Attach to default interactive desktop before any tests or imports create UI handles
import guard
guard.ensure_default_desktop()
guard.init_dpi_awareness()

try:
    import pythoncom
    pythoncom.CoInitialize()
except Exception:
    pass

