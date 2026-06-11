#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

# BUILD_TYPE: Release, Debug
BUILD_TYPE=Release

# Build support native cpu architecture (ON)/ all cpu architectures (OFF)
NATIVE_ARCS=ON

AI_ENGINE_DIR="${PROJECT_ROOT}/miloco_ai_engine/core"
BUILD_DIR="${PROJECT_ROOT}/build/ai_engine"
OUTPUT_DIR="${PROJECT_ROOT}/output"

rm -rf "${OUTPUT_DIR}"
mkdir -p "${BUILD_DIR}" "${OUTPUT_DIR}"

cmake -S "${AI_ENGINE_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_CXX_STANDARD=17 \
    -DCMAKE_RUNTIME_OUTPUT_DIRECTORY="${BUILD_DIR}/bin" \
    -DGGML_METAL=ON \
    -DGGML_METAL_EMBED_LIBRARY=ON \
    -DGGML_CUDA=OFF \
    -DGGML_NATIVE=${NATIVE_ARCS}

cmake --build "${BUILD_DIR}" --target llama-mico -j"$(sysctl -n hw.ncpu)"
cmake --install "${BUILD_DIR}" --prefix "${OUTPUT_DIR}"
