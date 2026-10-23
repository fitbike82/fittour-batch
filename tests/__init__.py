"""Expose the existing flat test suite to default unittest discovery."""
from pathlib import Path


def load_tests(loader, standard_tests, pattern):
    directory = str(Path(__file__).parent)
    return loader.discover(directory, pattern=pattern or "test*.py", top_level_dir=directory)
