#!/bin/bash
# Build lambda.zip with dependencies resolved FOR THE LAMBDA RUNTIME (Linux/CP3.11).
#
# Why this exists: a plain `pip install --platform manylinux2014_x86_64 ...` run on
# a Windows host evaluates environment markers (sys_platform) against the HOST, so
# mcp's `pywin32; sys_platform == "win32"` marker becomes unsatisfiable (no linux
# wheel for pywin32) and pip silently backtracks strands-agents to 1.1.0 instead of
# the 1.61.1 your venv uses. Resolution is therefore done with uv targeting linux,
# and the pinned result is installed with pip --no-deps (no marker re-evaluation).
set -e

PYTHON_VERSION="${1:-3.11}"
OUT="${2:-lambda.zip}"

command -v 7z >/dev/null 2>&1 || { echo "Error: 7z not found (https://www.7-zip.org)"; exit 1; }
command -v pip >/dev/null 2>&1 || { echo "Error: pip not found on PATH"; exit 1; }

rm -rf package "$OUT"
mkdir package

if command -v uv >/dev/null 2>&1; then
    uv pip compile requirements.txt --python-platform linux --python-version "$PYTHON_VERSION" -o req-linux.txt
    pip install -r req-linux.txt --no-deps --ignore-installed \
        --target=package \
        --platform manylinux2014_x86_64 \
        --python-version "$PYTHON_VERSION" \
        --implementation cp \
        --only-binary=:all:
else
    echo "Warning: uv not found - resolving with plain pip (only safe on a Linux host)."
    pip install -r requirements.txt \
        --target=package \
        --platform manylinux2014_x86_64 \
        --python-version "$PYTHON_VERSION" \
        --implementation cp \
        --only-binary=:all: --upgrade
fi

find package -type d -name "__pycache__" -exec rm -r {} + 2>/dev/null || true
find lambdas -type d -name "__pycache__" -exec rm -r {} + 2>/dev/null || true
cp -R lambdas/* package/

(cd package && 7z a "../$OUT" * >/dev/null)
echo "Built $OUT"
