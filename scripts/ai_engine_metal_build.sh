#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

# BUILD_TYPE: Release, Debug
BUILD_TYPE=Release

# Build for native Apple Silicon; override via CMAKE_OSX_ARCHITECTURES if cross-building.
OSX_ARCH=$(uname -m)

AI_ENGINE_DIR="${PROJECT_ROOT}/miloco_ai_engine/core"
BUILD_DIR="${PROJECT_ROOT}/build/ai_engine_metal"
OUTPUT_DIR="${PROJECT_ROOT}/output"

rm -rf "${OUTPUT_DIR}"
mkdir -p "${BUILD_DIR}" "${OUTPUT_DIR}"

cmake -S "${AI_ENGINE_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DGGML_METAL=ON \
    -DGGML_CUDA=OFF \
    -DGGML_NATIVE=ON \
    -DCMAKE_OSX_ARCHITECTURES=${OSX_ARCH}

cmake --build "${BUILD_DIR}" --target llama-mico -j"$(sysctl -n hw.ncpu)"
cmake --install "${BUILD_DIR}" --prefix "${OUTPUT_DIR}"
