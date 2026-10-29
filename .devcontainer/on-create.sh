#!/usr/bin/env bash
# Runs once when the codespace (or its prebuild) is created: Python env and model weights.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Python environment (.venv, CPU-only PyTorch)"
python -m venv .venv
.venv/bin/pip install -q --upgrade pip
# CPU wheels keep the environment under ~1 GB; the default Linux wheels pull several GB of CUDA libraries.
# torch comes from the CPU index without deps; its deps and everything else resolve from the default index.
# --no-compile: byte-compiling at install time fails on Windows bind mounts (local Dev Containers); imports compile lazily.
.venv/bin/pip install -q --no-compile --no-deps torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -q --no-compile -r .github/skills/cxr-analysis/requirements.txt
.venv/bin/pip check

echo "==> Model weights (~0.3 GB, cached in ~/.torchxrayvision)"
.venv/bin/python - <<'PY'
import torchxrayvision as xrv
for w in ("densenet121-res224-all", "densenet121-res224-pc"):
    xrv.models.DenseNet(weights=w)
xrv.baseline_models.chestx_det.PSPNet()
print("weights ready")
PY

echo "==> done"
