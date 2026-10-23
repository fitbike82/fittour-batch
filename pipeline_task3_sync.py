"""Compatibility entry point; implementation lives in scripts/pipeline_task3_sync.py."""
import importlib.util
from pathlib import Path
import sys

_spec = importlib.util.spec_from_file_location(__name__, Path(__file__).parent / 'scripts' / 'pipeline_task3_sync.py')
_module = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _module
_spec.loader.exec_module(_module)
