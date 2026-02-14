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
RUNTIME_DIR="${BUILD_DIR}/bin"

rm -rf "${OUTPUT_DIR}" "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}" "${OUTPUT_DIR}" "${RUNTIME_DIR}"

cmake -S "${AI_ENGINE_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_CXX_STANDARD=17 \
    -DCMAKE_CXX_STANDARD_REQUIRED=ON \
    -DCMAKE_CXX_EXTENSIONS=OFF \
    -DGGML_METAL=ON \
    -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=${RUNTIME_DIR}

cmake --build "${BUILD_DIR}" --target llama-mico -j"$(sysctl -n hw.ncpu)"
cmake --install "${BUILD_DIR}" --prefix "${OUTPUT_DIR}"
