from __future__ import annotations

import sys
from pathlib import Path


PLUGINS_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[4]

for import_root in (PROJECT_ROOT, PLUGINS_ROOT):
    import_root_text = str(import_root)
    if import_root_text not in sys.path:
        sys.path.insert(0, import_root_text)
