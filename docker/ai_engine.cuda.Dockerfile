# syntax=docker/dockerfile:1.4
ARG UBUNTU_VERSION=24.04
ARG CUDA_VERSION=12.5.1
# Target the CUDA build image.
ARG BASE_CUDA_DEV_CONTAINER=nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION}
# Target the CUDA run image.
ARG BASE_CUDA_RUN_CONTAINER=nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}
# Set apt repository.
# For Worldwide:
# - http://archive.ubuntu.com/ubuntu/
# For China:
# - https://mirrors.aliyun.com/ubuntu/
# - https://mirrors.tuna.tsinghua.edu.cn/ubuntu/
ARG APT_MIRRORS_URL=https://mirrors.tuna.tsinghua.edu.cn/ubuntu/
# Set pip index URL.
# For Worldwide: 
# - https://pypi.org/simple/
# For China: 
# - https://mirrors.aliyun.com/pypi/simple/
# - https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
ARG PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

################################################
# AI Engine Builder
################################################
FROM ${BASE_CUDA_DEV_CONTAINER} AS ai_engine-builder

# Restate apt mirrors repository.
ARG APT_MIRRORS_URL

WORKDIR /app

RUN set -eux \
    && sed -i "s|http://archive.ubuntu.com/ubuntu/|${APT_MIRRORS_URL}|g" /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y build-essential cmake git

COPY miloco_ai_engine/core /app/miloco_ai_engine/core
COPY third_party /app/third_party
COPY scripts/ai_engine_cuda_build.sh /app/scripts/ai_engine_cuda_build.sh

RUN bash /app/scripts/ai_engine_cuda_build.sh


################################################
# AI Engine Base
################################################
FROM ${BASE_CUDA_RUN_CONTAINER} AS ai_engine-base

# Restate apt mirrors repository.
ARG APT_MIRRORS_URL
# Restate PIP index URL.
ARG PIP_INDEX_URL

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY miloco_ai_engine/pyproject.toml /app/miloco_ai_engine/pyproject.toml

RUN set -eux \
    && sed -i "s|http://archive.ubuntu.com/ubuntu/|${APT_MIRRORS_URL}|g" /etc/apt/sources.list.d/ubuntu.sources \
    && apt update \
    && apt install -y curl python3 python3-pip \
    && pip config set global.index-url "${PIP_INDEX_URL}" \
    && pip install --no-build-isolation --break-system-packages /app/miloco_ai_engine \
    && rm -rf /app/miloco_ai_engine \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*


################################################
# AI Engine
################################################
FROM ai_engine-base AS ai_engine

ENV LD_LIBRARY_PATH=/app/output/lib:${LD_LIBRARY_PATH}

WORKDIR /app

COPY --from=ai_engine-builder /app/output /app/output
COPY config/ai_engine_config.yaml /app/config/ai_engine_config.yaml
COPY config/prompt_config.yaml /app/config/prompt_config.yaml
COPY miloco_ai_engine /app/miloco_ai_engine
COPY scripts/start_ai_engine.py /app/start_ai_engine.py

# Install project.
RUN pip install --no-build-isolation --break-system-packages -e /app/miloco_ai_engine

EXPOSE 8001

# Override by docker-compose, this is the default command.
# HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD curl -f "http://127.0.0.1:8001" || exit 1

CMD ["python3", "start_ai_engine.py"]
