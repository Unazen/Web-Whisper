#!/usr/bin/env bash
APP_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
source "$APP_DIR/.venv/bin/activate"
NVIDIA_LIBS="$(python - <<'PY'
import importlib.util

paths = []
for name in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        spec = None
    if spec and spec.submodule_search_locations:
        paths.append(list(spec.submodule_search_locations)[0])
print(":".join(paths))
PY
)"
if [ -n "$NVIDIA_LIBS" ]; then
  export LD_LIBRARY_PATH="$NVIDIA_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
export PYTHONUNBUFFERED=1
