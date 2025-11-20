#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

# BUILD_TYPE: Release, Debug
BUILD_TYPE=Release

# Build supports native GPU architecture (ON)/ all GPU architectures (OFF)
NATIVE_ARCH=OFF

AI_ENGINE_DIR="${PROJECT_ROOT}/miloco_ai_engine/core"
BUILD_DIR="${PROJECT_ROOT}/build/ai_engine"
OUTPUT_DIR="${PROJECT_ROOT}/output"

rm -rf "${OUTPUT_DIR}"
mkdir -p "${BUILD_DIR}" "${OUTPUT_DIR}"

cmake -S "${AI_ENGINE_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DGGML_CUDA=ON \
    -DGGML_NATIVE=${NATIVE_ARCH}

cmake --build "${BUILD_DIR}" --target llama-mico -j"$(nproc)"
cmake --install "${BUILD_DIR}" --prefix "${OUTPUT_DIR}"