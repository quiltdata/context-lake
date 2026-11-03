#!/usr/bin/env python3
"""Fail unless a release tag like v0.1.0 matches pyproject.toml and __version__."""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
tag = sys.argv[1] if len(sys.argv) > 1 else ''
project = re.search(r'(?m)^version = "([^"]+)"', (root / 'pyproject.toml').read_text())
module = re.search(r"(?m)^__version__ = ['\"]([^'\"]+)['\"]",
                   (root / 'src/context_lake/__init__.py').read_text())
versions = {'tag': tag.removeprefix('v'),
            'pyproject.toml': project and project.group(1),
            '__version__': module and module.group(1)}
if not tag.startswith('v') or len(set(versions.values())) != 1:
    sys.exit(f'release version mismatch: {versions}')
print(f"releasing {versions['tag']}")
