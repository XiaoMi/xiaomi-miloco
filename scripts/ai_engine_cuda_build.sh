#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

# BUILD_TYPE: Release, Debug
BUILD_TYPE=Release

# Build CUDA architectures. Please modify it according to your own needs if necessary. 
# For reference https://docs.nvidia.com/cuda/cuda-compiler-driver-nvcc/#gpu-feature-list
CUDA_ARCS="75;80;86;89;90" # default 20xx/30xx/40xx/50xx

# Build support native cpu architecture (ON)/ all cpu architectures (OFF)
NATIVE_ARCS=OFF

AI_ENGINE_DIR="${PROJECT_ROOT}/miloco_ai_engine/core"
BUILD_DIR="${PROJECT_ROOT}/build/ai_engine"
OUTPUT_DIR="${PROJECT_ROOT}/output"

rm -rf "${OUTPUT_DIR}"
mkdir -p "${BUILD_DIR}" "${OUTPUT_DIR}"

cmake -S "${AI_ENGINE_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCS} \
    -DGGML_CUDA=ON \
    -DGGML_NATIVE=${NATIVE_ARCS}

cmake --build "${BUILD_DIR}" --target llama-mico -j"$(nproc)"
cmake --install "${BUILD_DIR}" --prefix "${OUTPUT_DIR}"