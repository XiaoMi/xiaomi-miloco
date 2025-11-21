#!/bin/bash
# Xiaomi Miloco Script
set -euo pipefail
# English
# bash -c "$(wget -qO- https://xiaomi-miloco.cnbj1.mi-fds.com/xiaomi-miloco/install.sh)"
# wget -qO- https://xiaomi-miloco.cnbj1.mi-fds.com/xiaomi-miloco/install.sh | bash

# Script variables
PROJECT_NAME="Xiaomi Miloco"
PROJECT_CODE="miloco"
SCRIPT_VERSION="v0.0.7"
BACKEND_PORT=8000
AI_ENGINE_PORT=8001
MIRROR_GET_DOCKER="Aliyun" # Aliyun|AzureChinaCloud
# https://cdn.cnbj1.fds.api.mi-img.com/xiaomi-miloco
FDS_BASE_URL="${FDS_BASE_URL:-https://xiaomi-miloco.cnbj1.mi-fds.com/xiaomi-miloco}"
CDN_BASE_URL="${CDN_BASE_URL:-https://cdn.cnbj1.fds.api.mi-img.com/xiaomi-miloco}"
DOCKER_CMD="docker"
DOCKER_IMAGE_BACKEND_NAME="xiaomi/${PROJECT_CODE}-backend"
DOCKER_IMAGE_AI_ENGINE_NAME="xiaomi/${PROJECT_CODE}-ai_engine"
DOCKER_IMAGES=("${DOCKER_IMAGE_BACKEND_NAME}" "${DOCKER_IMAGE_AI_ENGINE_NAME}")
DOCKER_CONTAINERS=("${PROJECT_CODE}-backend" "${PROJECT_CODE}-ai_engine")

# Config path
PROJECT_HOME_DIR="${HOME}/.${PROJECT_CODE}"
PROJECT_CONFIG_FILE="${PROJECT_HOME_DIR}/${PROJECT_CODE}.conf"

SUPPORT_OS=("Linux")            # Linux, macOS
SUPPORT_OS_DISTRO=("ubuntu" "debian" "amzn" "fedora" "kylin" "rhel" "azl" "opensuse" "sles")
SUPPORT_ARCH=("x86_64")         # x86_64, arm64
SUPPORT_GPU_VENDOR=("NVIDIA")   # NVIDIA, AMD
MIN_GPU_MEMORY_GB=7.8

# Configuration
INSTALL_DIR="${HOME}"
INSTALL_FULL_DIR="${INSTALL_DIR}/${PROJECT_CODE}"
DOCKER_COMPOSE_FILE="docker-compose.yaml"
INSTALL_MODE="UnKnown"  # full, lite
INSTALL_FROM="Unknown"  # github, xiaomi-fds
MODELS_DL_FROM="Unknown" # modelscope, huggingface, xiaomi-fds

# NVIDIA Configuration
# https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html
# https://docs.nvidia.com/deeplearning/cudnn/backend/latest/reference/support-matrix.html
NVIDIA_MIN_DRIVER_VERSION="527.41"
NVIDIA_MIN_CUDA_VERSION="12.5.1"
NVIDIA_CUDA_TOOLKIT_VERSION="13-0"
# Get from https://developer.nvidia.com/cuda-downloads
SUPPORT_OS_DISTRO_NVIDIA=(
    "ubuntu24.04" "ubuntu22.04" "debian12"
    "amzn2023" "fedora42" "kylin10" "rhel8" "rhel9" "rhel10" "azl3"
    "opensuse15" "sles15"
)

# AMD Configure
SUPPORT_OS_DISTRO_AMD=()

# Models Download Config
MS_MIMO_VL_MILOCO_7B_Q4_0_URL="https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/resolve/master/MiMo-VL-Miloco-7B_Q4_0.gguf"
MS_MIMO_VL_MILOCO_7B_Q4_0_MMPROJ_URL="https://modelscope.cn/models/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/resolve/master/mmproj-MiMo-VL-Miloco-7B_BF16.gguf"
MS_QWEN3_8B_Q4_K_M_URL="https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/Qwen3-8B-Q4_K_M.gguf"
HF_MIMO_VL_MILOCO_7B_Q4_0_URL="https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/resolve/main/MiMo-VL-Miloco-7B_Q4_0.gguf"
HF_MIMO_VL_MILOCO_7B_Q4_0_MMPROJ_URL="https://huggingface.co/xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF/resolve/main/mmproj-MiMo-VL-Miloco-7B_BF16.gguf"
HF_QWEN3_8B_Q4_K_M_URL="https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf"

# System variables
OS="Unknown"
OS_VERSION="Unknown"
OS_VERSION_ID="Unknown"
OS_DISTRO="Unknown"
ARCH="Unknown"
KERNEL_VERSION="Unknown"
MEMORY=0
WSL_VERSION="Unknown"
GPU_VENDOR="Unknown"
GPU_MODEL="Unknown"
GPU_MEMORY=0

# Runtime environment
DEPEND_DOCKER="Unknown"
DEPEND_DOCKER_COMPOSE="Unknown"
DEPEND_GPU_MEMORY="Unknown"
# NVIDIA
DEPEND_NVIDIA_DRIVER="Unknown"          # TODO: >525.xx?
DEPEND_NVIDIA_CUDA_TOOLKIT="Unknown"    # TODO: >12.5.1?
DEPEND_NVCC_CUDA_TOOLKIT="Unknown"      # TODO: >12.5.1?
DEPEND_NVIDIA_CONTAINER_TOOLKIT="Unknown"
# AMD
DEPEND_AMD_DRIVER="Unknown"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print header
print_header() {
    clear
    echo -e "${BLUE}================================${NC}"
    echo -e "${BLUE}🏘️ ${PROJECT_NAME} ${SCRIPT_VERSION} ${NC}"
    echo -e "${BLUE}================================${NC}"
}

print_log(){
    echo "$1" >&2
}

print_log_e(){
    echo -e "$1" >&2
}

print_success() {
    echo -e "${GREEN}[✅ SUCCESS]${NC} $1" >&2
}

print_info() {
    echo -e "${BLUE}[ℹ️ INFO]${NC} $1" >&2
}

print_warning() {
    echo -e "${YELLOW}[⚠️ WARNING]${NC} $1" >&2
}

print_error() {
    echo -e "${RED}[❌ ERROR]${NC} $1" >&2
}

print_tip(){
    echo -e "${YELLOW}[💡 Tip]${NC} $1" >&2
}

print_dl(){
    echo -e "${BLUE}[🌐 Download]${NC} $1" >&2
}


check_root() {
    # Function to check if running as root
    if [[ $EUID -eq 0 ]]; then
        print_error "This script should not be run as root"
        exit 1
    fi
}

check_docker(){
    if ! command -v docker >/dev/null 2>&1; then
        return 1
    fi
    if docker info >/dev/null 2>&1; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        DOCKER_CMD="sudo docker"
        return 0
    fi
    return 1
}

check_port() {
    # 0: Port not occupied
    # 1: Port occupied
    # 2: invalid params
    # 3: Don't have commands
    local port="$1"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        echo 2
        return
    fi
    if command -v ss >/dev/null 2>&1; then
        if ss -tuln | grep -Eq "[.:]${port}[[:space:]]"; then
            echo 1
        else
            echo 0
        fi
        return
    fi
    if command -v lsof >/dev/null 2>&1; then
        if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            echo 1
        else
            echo 0
        fi
        return
    fi
    echo 3
}

version_compare() {
    # <func> v1 v2
    # v1 > v2 →  1
    # v1 = v2 →  0
    # v1 < v2 →  -1
    local v1=$1 v2=$2
    
    v1=$(echo "$v1" | tr -cd '0-9.')
    v2=$(echo "$v2" | tr -cd '0-9.')
    
    local IFS=.
    local -a ver1=($v1) ver2=($v2)
    local len=${#ver1[@]}
    if [ ${#ver2[@]} -gt $len ]; then
        len=${#ver2[@]}
    fi
    
    for ((i=0; i<$len; i++)); do
        local n1=${ver1[i]:-0}
        local n2=${ver2[i]:-0}
        
        if ((n1 > n2)); then
            echo 1
            return
            elif ((n1 < n2)); then
            echo -1
            return
        fi
    done
    echo 0
}

in_array() {
    local needle=$1; shift
    local haystack=("$@")
    for item in "${haystack[@]}"; do
        [[ "$item" == "$needle" ]] && return 0
    done
    return 1
}

is_number() {
    local input="$1"
    if [[ "$input" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
        return 0   # is a number
    else
        return 1   # not a number
    fi
}

print_system_info(){
    local print_mode="full"
    local un_supported=()
    
    if [ $# -ge 1 ]; then
        if [ "$1" == "lite" ]; then
            print_mode="lite"
        fi
    fi
    
    if ! in_array "$OS" "${SUPPORT_OS[@]}"; then
        un_supported+=("OS")
    fi
    if ! in_array "$OS_DISTRO" "${SUPPORT_OS_DISTRO[@]}"; then
        un_supported+=("OS_DISTRO")
    fi
    if ! in_array "$ARCH" "${SUPPORT_ARCH[@]}"; then
        un_supported+=("Architecture")
    fi
    
    print_info "System Information:"
    print_log "- OS: ${OS} (${OS_VERSION})"
    print_log "- Architecture: ${ARCH}"
    print_log "- Kernel Version: ${KERNEL_VERSION}"
    print_log "- Memory: ${MEMORY} GB"
    print_log "- WSL Version: ${WSL_VERSION}"
    
    if [ "${print_mode}" == "full" ]; then
        # Check if GPU is supported
        if ! in_array "$GPU_VENDOR" "${SUPPORT_GPU_VENDOR[@]}"; then
            un_supported+=("GPU Vendor")
        fi
        print_log "- GPU Vendor: ${GPU_VENDOR}"
    fi
    
    if [ ${#un_supported[@]} -gt 0 ]; then
        print_error "Unsupported system configuration:"
        for dep in "${un_supported[@]}"; do
            print_error "  - ${dep}"
        done
    else
        print_success "All system requirements are met"
    fi
}

is_supported_backend(){
    if ! in_array "${OS}" "${SUPPORT_OS[@]}"; then
        print_error "Current OS: ${OS}, only support ${SUPPORT_OS}"
        return -1
    fi
    if ! in_array "${OS_DISTRO}" "${SUPPORT_OS_DISTRO[@]}"; then
        print_error "Current OS Distro: ${OS_DISTRO}, only support ${SUPPORT_OS_DISTRO}"
        return -1
    fi
    if ! in_array "${ARCH}" "${SUPPORT_ARCH[@]}"; then
        print_error "Current Arch: ${ARCH}, only support ${SUPPORT_ARCH}"
        return -1
    fi
}

get_system_info() {
    print_info "Get system information..."
    # OS Information
    if [[ "${OSTYPE}" == "linux-gnu"* ]]; then
        OS="Linux"
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            OS_VERSION="${NAME} ${VERSION}"
            OS_VERSION_ID="${VERSION_ID}"
            OS_DISTRO="${ID}"
        else
            OS_VERSION="Unknown"
            OS_VERSION_ID="Unknown"
            OS_DISTRO="Unknown"
        fi
        elif [[ "${OSTYPE}" == "darwin"* ]]; then
        OS="macOS"
        OS_VERSION=$(sw_vers -productVersion)
    else
        OS="Unknown"
        OS_VERSION="Unknown"
    fi
    # Architecture
    ARCH=$(uname -m)
    # WSL Version
    get_wsl_version
    if [[ "$OS" == "Linux" ]]; then
        # Kernel version (for Linux)
        KERNEL_VERSION=$(uname -r)
        MEMORY=$(awk '/MemTotal/ {printf "%.2f", $2/1024/1024}' /proc/meminfo)
        if [ "${WSL_VERSION}" == "Unknown" ]; then
            # GPU Information
            if command -v lspci >/dev/null 2>&1; then
                if lspci | grep -i vga | grep -i nvidia >/dev/null 2>&1; then
                    GPU_VENDOR="NVIDIA"
                    GPU_MODEL=$(lspci | grep -i vga | grep -i nvidia | cut -d ':' -f3)
                    elif lspci | grep -i vga | grep -i amd >/dev/null 2>&1; then
                    GPU_VENDOR="AMD"
                    GPU_MODEL=$(lspci | grep -i vga | grep -i amd | cut -d ':' -f3)
                    elif lspci | grep -i vga | grep -i intel >/dev/null 2>&1; then
                    GPU_VENDOR="Intel"
                    GPU_MODEL=$(lspci | grep -i vga | grep -i intel | cut -d ':' -f3)
                else
                    GPU_VENDOR="Unknown"
                    GPU_MODEL="Unknown"
                fi
            else
                GPU_VENDOR="Unknown"
                GPU_MODEL="Unknown"
            fi
            elif [ "${WSL_VERSION}" == "WSL2" ]; then
            # Use nvidia-smi to check whether the NVIDIA GPU is supported
            if command -v nvidia-smi >/dev/null 2>&1; then
                GPU_VENDOR="NVIDIA"
                GPU_MODEL="Unknown"
            fi
        else
            GPU_VENDOR="Unknown"
            GPU_MODEL="Unknown"
        fi
        elif [ "$OS" = "Darwin" ]; then
        local TOTAL_MEM_BYTES=$(sysctl -n hw.memsize)
        KERNEL_VERSION="Unknown"
        MEMORY=$(awk -v bytes="$TOTAL_MEM_BYTES" 'BEGIN {printf "%.2f", bytes/1024/1024/1024}')
    else
        KERNEL_VERSION="Unknown"
        MEMORY=0
    fi
}

get_wsl_version() {
    local kernel_release=$(uname -r)
    local proc_version=$(cat /proc/version)
    if echo "$proc_version" | grep -qi microsoft; then
        if echo "$kernel_release" | grep -q "WSL2"; then
            WSL_VERSION="WSL2"
        else
            WSL_VERSION="WSL1"
        fi
    else
        WSL_VERSION="Unknown"
    fi
}

print_runtime_environment(){
    local print_mode="full"
    local missing_deps=()
    
    if [ $# -ge 1 ]; then
        if [ "$1" == "lite" ]; then
            print_mode="lite"
        fi
    fi
    
    if [ "${DEPEND_DOCKER}" == "Unknown" ]; then
        missing_deps+=("Docker")
    fi
    if [ "${DEPEND_DOCKER_COMPOSE}" == "Unknown" ]; then
        missing_deps+=("Docker Compose")
    fi
    
    print_info "Runtime Environment:"
    print_log "- Docker: ${DEPEND_DOCKER}"
    print_log "- Docker Compose: ${DEPEND_DOCKER_COMPOSE}"
    
    
    if [ "${print_mode}" == "full" ]; then
        if [ "${DEPEND_GPU_MEMORY}" == "Unknown" ]; then
            missing_deps+=("GPU Memory < ${MIN_GPU_MEMORY_GB}")
        fi
        
        if [ "$GPU_VENDOR" = "NVIDIA" ]; then
            if [ "${DEPEND_NVIDIA_DRIVER}" == "Unknown" ]; then
                missing_deps+=("NVIDIA Driver")
            fi
            # if [ "${DEPEND_NVIDIA_CUDA_TOOLKIT}" == "Unknown" ]; then
            #     missing_deps+=("NVIDIA CUDA Toolkit")
            # fi
            if [ "${DEPEND_NVIDIA_CONTAINER_TOOLKIT}" == "Unknown" ]; then
                missing_deps+=("NVIDIA Container Toolkit")
            fi
            print_log "- GPU Model: ${GPU_MODEL}"
            print_log "- NVIDIA Driver: ${DEPEND_NVIDIA_DRIVER}"
            print_log "- GPU MEMORY: ${GPU_MEMORY} GB"
            print_log "- CUDA Toolkit: ${DEPEND_NVIDIA_CUDA_TOOLKIT}"
            print_log "- NVIDIA Container Toolkit: ${DEPEND_NVIDIA_CONTAINER_TOOLKIT}"
            elif [ "$GPU_VENDOR" = "AMD" ]; then
            if [ "${DEPEND_AMD_DRIVER}" == "Unknown" ]; then
                missing_deps+=("AMD Driver")
            fi
            print_log "- AMD Driver: ${DEPEND_AMD_DRIVER}"
            print_log "- GPU MEMORY: ${GPU_MEMORY} GB"
        fi
    fi
    
    
    if [ ${#missing_deps[@]} -gt 0 ]; then
        print_error "Missing dependencies:"
        for dep in "${missing_deps[@]}"; do
            print_error "  - ${dep}"
        done
    else
        print_success "All runtime environment requirements are met"
    fi
    
    if [ "${GPU_VENDOR}" == "Unknown" ] && [ "${WSL_VERSION}" == "WSL2" ]; then
        print_tip "Detected that you are currently in a ${YELLOW}WSL2${NC} environment. If you use ${YELLOW}NVIDIA GPU${NC}, \nyou can try to download the ${YELLOW}NVIDIA APP${NC} first, Install the driver and continue."
        print_log_e "  🌐 Worldwide: https://www.nvidia.com/en-us/software/nvidia-app/"
        print_log_e "  🌐 中国大陆: https://www.nvidia.cn/software/nvidia-app/"
    fi
}

is_supported_ai_engine() {
    if [ "${DEPEND_GPU_MEMORY}" == "Unknown" ]; then
        return 1
    fi
    if [ "$GPU_VENDOR" == "NVIDIA" ]; then
        if [ "${DEPEND_NVIDIA_DRIVER}" == "Unknown" ]; then
            return 1
        fi
        # if [ "${DEPEND_NVIDIA_CUDA_TOOLKIT}" == "Unknown" ]; then
        #     return 1
        # fi
        if [ "${DEPEND_NVIDIA_CONTAINER_TOOLKIT}" == "Unknown" ]; then
            return 1
        fi
        if ! in_array "${OS_DISTRO}${OS_VERSION_ID}" "${SUPPORT_OS_DISTRO_NVIDIA[@]}"; then
            print_error "${GPU_VENDOR} not support ${OS_DISTRO}${OS_VERSION_ID}"
            return 1
        fi
        elif [ "$GPU_VENDOR" == "AMD" ]; then
        if [ "${DEPEND_AMD_DRIVER}" == "Unknown" ]; then
            return 1
        fi
        if ! in_array "${OS_DISTRO}${OS_VERSION_ID}" "${SUPPORT_OS_DISTRO_AMD[@]}"; then
            print_error "${GPU_VENDOR} not support ${OS_DISTRO}${OS_VERSION_ID}"
            return 1
        fi
    fi
    return 0
}

get_runtime_environment(){
    print_info "Get runtime environment..."
    
    # Check Docker
    if ! command -v ${DOCKER_CMD} >/dev/null 2>&1; then
        DEPEND_DOCKER="Unknown"
    else
        DEPEND_DOCKER="$(${DOCKER_CMD} --version | awk '{print $3}')"
        DEPEND_DOCKER="${DEPEND_DOCKER%,}"
    fi
    # Check Docker Compose
    if ! command -v ${DOCKER_CMD} compose >/dev/null 2>&1 && ! ${DOCKER_CMD} compose version >/dev/null 2>&1; then
        DEPEND_DOCKER_COMPOSE="Unknown"
    else
        DEPEND_DOCKER_COMPOSE="$(${DOCKER_CMD} compose version | awk '{print $4}')"
    fi
    # Check NVIDIA drivers (if GPU available)
    if [[ "$GPU_VENDOR" == "NVIDIA" ]]; then
        if ! command -v nvidia-smi >/dev/null 2>&1; then
            DEPEND_NVIDIA_DRIVER="Unknown"
            DEPEND_NVIDIA_CUDA_TOOLKIT="Unknown"
            DEPEND_GPU_MEMORY="Unknown"
        else
            if ! DEPEND_NVIDIA_DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null); then
                print_log "Unable to obtain NVIDIA driver version with nvidia-smi"
                DEPEND_NVIDIA_DRIVER="Unknown"
            fi
            if ! DEPEND_NVIDIA_CUDA_TOOLKIT=$(nvidia-smi | awk -F'CUDA Version: ' '{print $2}' | awk '{print $1}' | tr -d '[:space:]'); then
                print_log "Unable to obtain CUDA Toolkit driver version with nvidia-smi"
                DEPEND_NVIDIA_CUDA_TOOLKIT="Unknown"
            fi
            # Get first GPU memory
            # TODO: Get all GPUs
            if mem_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 2>/dev/null); then
                GPU_MEMORY=$(echo "$mem_mb" | awk '{printf "%.2f", $1/1024}')
                if (( $(echo "$GPU_MEMORY >= ${MIN_GPU_MEMORY_GB}" | bc -l) )); then
                    DEPEND_GPU_MEMORY="${GPU_MEMORY} GB"
                else
                    DEPEND_GPU_MEMORY="Unknown"
                fi
            else
                print_log "Unable to obtain NVIDIA Memory with nvidia-smi"
                DEPEND_GPU_MEMORY="Unknown"
            fi
            # Update GPU_MODEL with nvidia-smi
            if gpu_model_new=$(nvidia-smi --query-gpu=name --format=csv,noheader,nounits | head -n1 2>/dev/null); then
                GPU_MODEL="${gpu_model_new}"
            fi
            # The nvidia-smi command can be executed, but the driver version and video memory cannot be obtained. The user is prompted to restart the computer and try again.
            if [ "${DEPEND_NVIDIA_DRIVER}" == "Unknown" ] && [ "${DEPEND_GPU_MEMORY}" == "Unknown" ]; then
                print_warning "It is detected that nvidia-smi cannot communicate with the GPU driver. ${YELLOW}Please try restarting the computer to continue${NC}.\n"
                read -rp "[✳️ OPTION]  Do you want to continue? (yes/No): "
                if [ "${REPLY}" != "yes" ]; then
                    print_tip "Installation cancelled"
                    return 0
                fi
            fi
        fi
        
        # If the cuda version is not obtained, try using the nvcc command
        if ! command -v nvcc >/dev/null 2>&1; then
            DEPEND_NVCC_CUDA_TOOLKIT="Unknown"
        else
            DEPEND_NVCC_CUDA_TOOLKIT=$(nvcc --version | grep release | awk '{print $6}' | tr -d ',')
        fi
        
        if ! command -v nvidia-ctk >/dev/null 2>&1; then
            DEPEND_NVIDIA_CONTAINER_TOOLKIT="Unknown"
        else
            DEPEND_NVIDIA_CONTAINER_TOOLKIT="$(nvidia-ctk --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
        fi
    fi
}

print_service_status(){
    print_info "Service status: "
    if ! is_service_installed; then
        print_log_e "- ${RED}Service not installed${NC}"
    else
        if is_service_running; then
            print_log_e "- ${GREEN}Service running${NC}"
        else
            print_log_e "- ${YELLOW}Service Stopped${NC}"
        fi
        print_log_e "-- Install Directory: ${GREEN}${INSTALL_FULL_DIR}${NC}"
        print_log_e "-- Install Mode     : ${GREEN}${INSTALL_MODE}${NC}"
    fi
    if [ "${DEPEND_DOCKER}" != "Unknown" ]; then
        # Get docker images and container
        print_log "- Docker images:"
        for image in "${DOCKER_IMAGES[@]}"; do
            ${DOCKER_CMD} images --format '{{.Repository}} {{.Tag}} {{.ID}} {{.Size}} {{.CreatedAt}}' \
            | grep "^${image} " \
            | while read -r repo tag id size created; do
                printf "%-26s %-10s %-13s %-8s %-40s\n" "-- ${repo}" "${tag}" "${id}" "${size}" "${created}"
            done || print_log_e "-- ${RED}Images not found: ${image}${NC}"
        done
        print_log "- Docker containers:"
        for container in "${DOCKER_CONTAINERS[@]}"; do
            ${DOCKER_CMD} ps -a --format '{{.Names}} {{.Image}} {{.ID}} {{.RunningFor}}' \
            | grep "^${container} " \
            | while read -r name image cid runningfor; do
                printf "%-22s %-30s %-13s %-15s\n" "-- ${name}" "${image}" "${cid}" "${runningfor}"
            done || print_log_e "-- ${RED}Container not found: ${container}${NC}"
        done
    fi
}

get_valid_port(){
    local default_port="$1"
    local service_name="$2"
    local in_port="Unknown"
    while true; do
        read -rp "[✳️ INPUT] Please enter the ${service_name} service port (Default: $1): " in_port
        if [ -z "${in_port}" ]; then
            in_port="$1"
        fi
        if ! is_number "${in_port}"; then
            print_error "Invalid port number: ${in_port}, please enter a valid number"
            continue;
        fi
        local check_result=$(check_port "${in_port}")
        if [ "${check_result}" -eq 0 ]; then
            break;
            elif [ "${check_result}" -eq 1 ]; then
            print_error "Port ${in_port} is already in use, please enter another one"
            continue;
            elif [ "${check_result}" -eq 2 ]; then
            print_error "Port ${in_port} is invalid, please enter a valid number"
            continue;
        fi
    done
    print_log_e "Using port ${GREEN}${in_port}${NC} for ${service_name}"
    echo "${in_port}"
}

install_service_from() {
    print_info "Install Service from: "
    print_info "1. GitHub Packages"
    print_info "2. Xiaomi FDS"
    while true; do
        read -rp "[✳️ INPUT] Please select the installation source (1/2): " in_source
        case $in_source in
            1)
                print_log_e "Selected: ${GREEN}1. GitHub Packages${NC}"
                INSTALL_FROM="github"
                return
            ;;
            2)
                print_log_e "Selected: ${GREEN}2. Xiaomi FDS${NC}"
                INSTALL_FROM="xiaomi-fds"
                return
            ;;
            *)
                print_error "Invalid option, please select again"
            ;;
        esac
    done
}

download_models_from() {
    print_info "Download Models from: "
    print_info "1. Model Scope"
    print_info "2. Hugging Face"
    print_info "3. Xiaomi FDS"
    while true; do
        read -rp "[✳️ INPUT] Please select the installation source (1/2/3): " in_source
        case $in_source in
            1)
                print_log_e "Selected: ${GREEN}1. Model Scope${NC}"
                MODELS_DL_FROM="modelscope"
                return
            ;;
            2)
                print_log_e "Selected: ${GREEN}2. Hugging Face${NC}"
                MODELS_DL_FROM="huggingface"
                return
            ;;
            3)
                print_log_e "Selected: ${GREEN}3. Xiaomi FDS${NC}"
                MODELS_DL_FROM="xiaomi-fds"
                return
            ;;
            *)
                print_error "Invalid option, please select again"
            ;;
        esac
    done
}

get_service_config() {
    local key="$1"
    local file="$2"
    
    if [ ! -f "$file" ]; then
        echo "Unknown"
        return
    fi
    
    local value
    value=$(grep -E "^${key}=" "$file" | cut -d'=' -f2-)
    
    if [ -z "$value" ]; then
        echo "Unknown"
    else
        echo "$value"
    fi
}

set_service_config() {
    local key="$1"
    local value="$2"
    local file="$3"
    
    if [ ! -f "$file" ]; then
        echo "${key}=${value}" > "$file"
        return
    fi
    
    if grep -qE "^${key}=" "$file"; then
        sed -i "s#^${key}=.*#${key}=${value}#g" "$file"
    else
        echo "${key}=${value}" >> "$file"
    fi
}

is_service_installed() {
    # Get service config and check.
    INSTALL_DIR=$(get_service_config "INSTALL_DIR" "${PROJECT_CONFIG_FILE}")
    INSTALL_MODE=$(get_service_config "INSTALL_MODE" "${PROJECT_CONFIG_FILE}")
    INSTALL_FROM=$(get_service_config "INSTALL_FROM" "${PROJECT_CONFIG_FILE}")
    # print_log "INSTALL_DIR: ${INSTALL_DIR}, INSTALL_MODE: ${INSTALL_MODE}"
    if [[ "${INSTALL_DIR}" == "Unknown" || "${INSTALL_MODE}" == "Unknown" || "${INSTALL_FROM}" == "Unknown" ]]; then
        return 1
    fi
    INSTALL_FULL_DIR="${INSTALL_DIR}/${PROJECT_CODE}"
    if [ -d "${INSTALL_FULL_DIR}" ] && [ -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" ]; then
        # print_log "Service is installed at ${INSTALL_FULL_DIR}, mode: ${INSTALL_MODE}"
        return 0
    else
        return 1
    fi
}

is_service_running() {
    if ! is_service_installed; then
        return 1
    fi
    local installed_status=$(${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" ps --services --filter "status=running")
    if [ -n "${installed_status}" ]; then
        # print_log "Service is running"
        return 0
    fi
    return 1
}

download_models() {
    # TODO: Use download script, and check md5
    if [ "${INSTALL_MODE}" != "full" ]; then
        print_log "Install without AI Engine, skipping model download"
        return 0
    fi
    if [ ! -d "${INSTALL_FULL_DIR}/models" ]; then
        mkdir -p "${INSTALL_FULL_DIR}/models"
    fi
    mkdir -p "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B"
    mkdir -p "${INSTALL_FULL_DIR}/models/Qwen3-8B"
    
    print_log "Downloading models..."
    if [ "${MODELS_DL_FROM}" == "modelscope" ]; then
        wget -c -O "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B/MiMo-VL-Miloco-7B_Q4_0.gguf" "${MS_MIMO_VL_MILOCO_7B_Q4_0_URL}"
    else
        wget -c -O "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B/MiMo-VL-Miloco-7B_Q4_0.gguf" "${HF_MIMO_VL_MILOCO_7B_Q4_0_URL}"
    fi
    print_dl "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B/MiMo-VL-Miloco-7B_Q4_0.gguf"
    
    if [ "${MODELS_DL_FROM}" == "modelscope" ]; then
        wget -c -O "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B/mmproj-MiMo-VL-Miloco-7B_BF16.gguf" "${MS_MIMO_VL_MILOCO_7B_Q4_0_MMPROJ_URL}"
    else
        wget -c -O "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B/mmproj-MiMo-VL-Miloco-7B_BF16.gguf" "${HF_MIMO_VL_MILOCO_7B_Q4_0_MMPROJ_URL}"
    fi
    print_dl "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B/mmproj-MiMo-VL-Miloco-7B_BF16.gguf"
    
    if [ "${MODELS_DL_FROM}" == "modelscope" ]; then
        wget -c -O "${INSTALL_FULL_DIR}/models/Qwen3-8B/Qwen3-8B-Q4_K_M.gguf" "${MS_QWEN3_8B_Q4_K_M_URL}"
    else
        wget -c -O "${INSTALL_FULL_DIR}/models/Qwen3-8B/Qwen3-8B-Q4_K_M.gguf" "${HF_QWEN3_8B_Q4_K_M_URL}"
    fi
    print_dl "${INSTALL_FULL_DIR}/models/Qwen3-8B/Qwen3-8B-Q4_K_M.gguf"
}

download_models_fds() {
    # TODO: Use download script
    if [ "${INSTALL_MODE}" != "full" ]; then
        print_log "Install without AI Engine, skipping model download"
        return 0
    fi
    local need_dl="no"
    if [ ! -d "${INSTALL_FULL_DIR}/models" ]; then
        need_dl="yes"
    fi
    if [ ! -f "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B/MiMo-VL-Miloco-7B_Q4_0.gguf" ]; then
        need_dl="yes"
    fi
    if [ ! -f "${INSTALL_FULL_DIR}/models/MiMo-VL-Miloco-7B/mmproj-MiMo-VL-Miloco-7B_BF16.gguf" ]; then
        need_dl="yes"
    fi
    if [ ! -f "${INSTALL_FULL_DIR}/models/Qwen3-8B/Qwen3-8B-Q4_K_M.gguf" ]; then
        need_dl="yes"
    fi
    
    if [ "${need_dl}" == "yes" ] ; then
        print_log "Downloading models..."
        wget -c -O "${INSTALL_FULL_DIR}/models.zip" "${CDN_BASE_URL}/models.zip"
        wget -O "${INSTALL_FULL_DIR}/models.md5" "${FDS_BASE_URL}/models.md5"
        # Checking md5
        print_log "Checking md5..."
        local md5_calc=$(md5sum "${INSTALL_FULL_DIR}/models.zip" | awk '{print $1}')
        local md5_cloud=$(tr -d ' \n\r\t' < "${INSTALL_FULL_DIR}/models.md5")
        if [ "$md5_calc" != "$md5_cloud" ]; then
            print_error "models MD5 mismatch: ${md5_calc} != ${md5_cloud}"
            exit 1
        else
            print_success "models MD5 match: ${md5_calc} == ${md5_cloud}"
        fi
        print_success "Download models successfully: ${INSTALL_FULL_DIR}/models.zip"
        print_log "Unzip models..."
        rm -rf "${INSTALL_FULL_DIR}/models"
        unzip "${INSTALL_FULL_DIR}/models.zip" -d "${INSTALL_FULL_DIR}/models"
        print_success "Unzip models successfully: ${INSTALL_FULL_DIR}/models"
    else
        print_log "Skip download, models directory exists: ${INSTALL_FULL_DIR}/models"
    fi
}

download_docker_images() {
    print_log "Downloading docker images..."
    
    wget -O "${INSTALL_FULL_DIR}/.latest_version_cloud" "${FDS_BASE_URL}/latest_version.txt"
    # Get latest version
    local latest_version="v0.0.0"
    local cloud_version=$(xargs < "${INSTALL_FULL_DIR}/.latest_version_cloud")
    if [ -f "${INSTALL_FULL_DIR}/.latest_version" ]; then
        latest_version=$(xargs < "${INSTALL_FULL_DIR}/.latest_version")
        if [ $(version_compare "${latest_version}" "${cloud_version}") -gt 0 ]; then
            print_info "No latest version available, skip downloading updates: ${latest_version} > ${cloud_version}"
            rm -rf "${INSTALL_FULL_DIR}/.latest_version_cloud"
            return 0
        else
            mv "${INSTALL_FULL_DIR}/.latest_version_cloud" "${INSTALL_FULL_DIR}/.latest_version"
            latest_version="${cloud_version}"
        fi
    else
        mv "${INSTALL_FULL_DIR}/.latest_version_cloud" "${INSTALL_FULL_DIR}/.latest_version"
        latest_version="${cloud_version}"
    fi
    
    wget -c -O "${INSTALL_FULL_DIR}/${latest_version}.zip" "${CDN_BASE_URL}/images/${latest_version}.zip"
    wget -O "${INSTALL_FULL_DIR}/${latest_version}.md5" "${CDN_BASE_URL}/images/${latest_version}.md5"
    print_log "Checking md5..."
    local md5_calc=$(md5sum "${INSTALL_FULL_DIR}/${latest_version}.zip" | awk '{print $1}')
    local md5_cloud=$(tr -d ' \n\r\t' < "${INSTALL_FULL_DIR}/${latest_version}.md5")
    if [ "$md5_calc" != "$md5_cloud" ]; then
        print_error "${latest_version}.zip MD5 mismatch: ${md5_calc} != ${md5_cloud}, please retry"
        rm -rf "${INSTALL_FULL_DIR}/${latest_version}.zip"
        exit 1
    else
        print_success "${latest_version}.zip MD5 match: ${md5_calc} == ${md5_cloud}"
    fi
    rm -rf "${INSTALL_FULL_DIR}/${latest_version}"
    unzip "${INSTALL_FULL_DIR}/${latest_version}.zip" -d "${INSTALL_FULL_DIR}/${latest_version}"
    print_log "Loading ${latest_version} docker images..."
    ${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" down || true
    ${DOCKER_CMD} rmi "${DOCKER_IMAGE_BACKEND_NAME}:${latest_version}" 2>/dev/null || true
    ${DOCKER_CMD} rmi "${DOCKER_IMAGE_BACKEND_NAME}:latest" 2>/dev/null || true
    ${DOCKER_CMD} load -i "${INSTALL_FULL_DIR}/${latest_version}/backend.tar"
    if ${DOCKER_CMD} image inspect "${DOCKER_IMAGE_BACKEND_NAME}:${latest_version}" >/dev/null 2>&1; then
        ${DOCKER_CMD} tag "${DOCKER_IMAGE_BACKEND_NAME}:${latest_version}" "${DOCKER_IMAGE_BACKEND_NAME}:latest"
    fi
    
    if [ "${INSTALL_MODE}" == "full" ]; then
        ${DOCKER_CMD} rmi "${DOCKER_IMAGE_AI_ENGINE_NAME}:${latest_version}" 2>/dev/null || true
        ${DOCKER_CMD} rmi "${DOCKER_IMAGE_AI_ENGINE_NAME}:latest" 2>/dev/null || true
        ${DOCKER_CMD} load -i "${INSTALL_FULL_DIR}/${latest_version}/ai_engine.tar"
        if ${DOCKER_CMD} image inspect "${DOCKER_IMAGE_AI_ENGINE_NAME}:${latest_version}" >/dev/null 2>&1; then
            ${DOCKER_CMD} tag "${DOCKER_IMAGE_AI_ENGINE_NAME}:${latest_version}" "${DOCKER_IMAGE_AI_ENGINE_NAME}:latest"
        fi
    fi
    
    rm -rf "${INSTALL_FULL_DIR}/${latest_version}"
    print_success "Docker images loaded successfully"
}

config_install_env(){
    # TODO: Use dependency manager, adapter multi platform
    case "${OS_DISTRO}" in
        ubuntu|debian)
            sudo apt-get update
            sudo apt-get install -y wget unzip gcc bc
        ;;
        amzn|fedora|kylin|rhel)
        ;;
        opensuse|sles)
        ;;
        *)
            print_error "Un-support distro: ${OS_DISTRO}"
        ;;
    esac
}

quick_install() {
    print_header
    print_info "Performing quick installation..."
    check_root
    
    # Check runtime environment
    get_system_info
    get_runtime_environment
    print_system_info
    print_runtime_environment
    
    if ! is_supported_backend; then
        return 0
    fi
    
    # Check if already installed
    if is_service_installed; then
        print_warning "The service has been installed. Reinstallation will overwrite the original files"
        print_log_e "Install Directory: ${YELLOW}${INSTALL_FULL_DIR}${NC}"
        print_log_e "Install Mode     : ${YELLOW}${INSTALL_MODE}${NC}"
        read -rp "[✳️ OPTION]  Do you want to reinstall? (yes/No): "
        if [ "${REPLY}" != "yes" ]; then
            print_tip "Re-installation cancelled"
            return 0
        fi
        # Stop service
        stop_service
    else
        INSTALL_DIR="${HOME}"
        INSTALL_FULL_DIR="${INSTALL_DIR}/${PROJECT_CODE}"
    fi
    
    local install_dir_new="Unknown"
    print_log ""
    read -rp "[✳️ INPUT] Please enter install directory [Default: ${INSTALL_DIR}]: " install_dir_new
    if [ -z "${install_dir_new}" ]; then
        install_dir_new="${INSTALL_DIR}"
        print_log_e "Using default install directory: ${GREEN}${install_dir_new}${NC}"
    else
        print_log_e "Using custom install directory: ${GREEN}${install_dir_new}${NC}"
    fi
    if [ ! -d "${install_dir_new}" ]; then
        print_error "Install directory does not exist: ${install_dir_new}"
        return 1
    fi
    
    if [ ! -r "${install_dir_new}" ] || [ ! -w "${install_dir_new}" ]; then
        print_error "Install directory is not readable or writable: ${install_dir_new}"
        return 1
    fi
    
    INSTALL_DIR="${install_dir_new}"
    INSTALL_FULL_DIR="${INSTALL_DIR}/${PROJECT_CODE}"
    INSTALL_MODE="full"
    mkdir -p "${INSTALL_FULL_DIR}"
    
    # Check backend port
    BACKEND_PORT=$(get_valid_port "${BACKEND_PORT}" "Miloco Back-end")
    # Check AI Engine port
    AI_ENGINE_PORT=$(get_valid_port "${AI_ENGINE_PORT}" "Miloco AI Engine")
    
    if [ "${BACKEND_PORT}" == "${AI_ENGINE_PORT}" ]; then
        print_error "The AI Engine and Backend service are using the same port ${RED}${BACKEND_PORT}${NC}, please try again."
        return 1
    fi
    
    install_service_from
    
    config_install_env
    
    # Create configuration directory
    print_log "Set configuration..."
    mkdir -p "${PROJECT_HOME_DIR}"
    set_service_config "INSTALL_DIR" "${INSTALL_DIR}" "${PROJECT_CONFIG_FILE}"
    set_service_config "INSTALL_MODE" "${INSTALL_MODE}" "${PROJECT_CONFIG_FILE}"
    set_service_config "INSTALL_FROM" "${INSTALL_FROM}" "${PROJECT_CONFIG_FILE}"
    
    if ! install_runtime_environment; then
        print_error "Failed to install runtime environment"
        return 1
    fi
    
    get_runtime_environment
    
    if ! is_supported_ai_engine; then
        print_runtime_environment
        print_error "System unsupported MILOCO AI Engine, please install without AI Engine"
        return 0
    fi
    
    install_service
    stop_service
    start_service
}

quick_install_lite() {
    print_header
    print_info "Performing quick installation(Without AI Engine)..."
    check_root
    
    # Check runtime environment
    get_system_info
    get_runtime_environment
    print_system_info "lite"
    print_runtime_environment "lite"
    
    if ! is_supported_backend; then
        return 0
    fi
    
    # Check if already installed
    if is_service_installed; then
        print_warning "The service has been installed. Reinstallation will overwrite the original files"
        print_log_e "- Install Directory: ${YELLOW}${INSTALL_FULL_DIR}${NC}"
        print_log_e "- Install Mode     : ${YELLOW}${INSTALL_MODE}${NC}"
        read -rp "[✳️ OPTION]  Do you want to reinstall? (yes/No): "
        if [ "${REPLY}" != "yes" ]; then
            print_tip "Re-installation cancelled"
            return 0
        fi
        # Stop service
        stop_service
    else
        INSTALL_DIR="${HOME}"
        INSTALL_FULL_DIR="${INSTALL_DIR}/${PROJECT_CODE}"
    fi
    
    local install_dir_new="Unknown"
    print_log ""
    read -rp "[✳️ INPUT] Please enter install directory [Default: ${INSTALL_DIR}]: " install_dir_new
    if [ -z "${install_dir_new}" ]; then
        install_dir_new="${INSTALL_DIR}"
        print_log_e "Using default install directory: ${GREEN}${install_dir_new}${NC}"
    else
        print_log_e "Using custom install directory: ${GREEN}${install_dir_new}${NC}"
    fi
    if [ ! -d "${install_dir_new}" ]; then
        print_error "Install directory does not exist: ${install_dir_new}"
        return 1
    fi
    
    if [ ! -r "${install_dir_new}" ] || [ ! -w "${install_dir_new}" ]; then
        print_error "Install directory is not readable or writable: ${install_dir_new}"
        return 1
    fi
    
    INSTALL_DIR="${install_dir_new}"
    INSTALL_FULL_DIR="${INSTALL_DIR}/${PROJECT_CODE}"
    INSTALL_MODE="lite"
    mkdir -p "${INSTALL_FULL_DIR}"
    
    # Check backend port
    BACKEND_PORT=$(get_valid_port "${BACKEND_PORT}" "Miloco Back-end")
    
    install_service_from
    
    config_install_env
    
    # Create configuration directory
    print_log "Set configuration..."
    mkdir -p "${PROJECT_HOME_DIR}"
    set_service_config "INSTALL_DIR" "${INSTALL_DIR}" "${PROJECT_CONFIG_FILE}"
    set_service_config "INSTALL_MODE" "${INSTALL_MODE}" "${PROJECT_CONFIG_FILE}"
    set_service_config "INSTALL_FROM" "${INSTALL_FROM}" "${PROJECT_CONFIG_FILE}"
    
    if ! install_runtime_environment; then
        print_error "Failed to install runtime environment"
        return 1
    fi
    
    get_runtime_environment
    
    install_service
    stop_service
    start_service
}

install_service(){
    # Create installation directory
    print_log "Start installation..."
    mkdir -p "${INSTALL_FULL_DIR}"
    
    print_log "Get docker compose file..."
    if [ -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" ]; then
        mv "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}.bak"
        print_info "Backup ${DOCKER_COMPOSE_FILE} file: ${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}.bak"
    fi
    if [ -f "${INSTALL_FULL_DIR}/.env" ]; then
        mv "${INSTALL_FULL_DIR}/.env" "${INSTALL_FULL_DIR}/.env.bak"
        print_info "Backup .env file: ${INSTALL_FULL_DIR}/.env.bak"
    fi
    wget -O "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" "${FDS_BASE_URL}/docker-compose-${INSTALL_MODE}.yaml"
    print_log "Get docker-compose.yaml completed: ${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}"
    wget -O "${INSTALL_FULL_DIR}/.env" "${FDS_BASE_URL}/.env.example"
    print_log "Get .env completed: ${INSTALL_FULL_DIR}/.env"
    # Replace .env variables
    sed -i "s/^BACKEND_PORT=.*/BACKEND_PORT=${BACKEND_PORT}/" "${INSTALL_FULL_DIR}/.env"
    if [ "${INSTALL_MODE}" == "full" ]; then
        sed -i "s/^AI_ENGINE_PORT=.*/AI_ENGINE_PORT=${AI_ENGINE_PORT}/" "${INSTALL_FULL_DIR}/.env"
    fi
    if [ "${INSTALL_FROM}" == "xiaomi-fds" ]; then
        sed -i 's/^DOCKER_REPO=ghcr\.io\//#DOCKER_REPO=ghcr.io\//' "${INSTALL_FULL_DIR}/.env"
    fi
    
    if [ "${INSTALL_MODE}" == "full" ]; then
        download_models_from
        if [ "${MODELS_DL_FROM}" == "xiaomi-fds" ]; then
            download_models_fds
        else
            download_models
        fi
    fi
    
    print_success "${PROJECT_NAME} installation completed successfully!"
    print_success "Service installed at: ${INSTALL_FULL_DIR}"
}

# Start Service
start_service() {
    if ! is_service_installed; then
        print_error "Service not installed"
        return 0
    fi
    if is_service_running; then
        print_log "Service already running"
        return 0
    fi
    print_log "Starting service..."
    if [ "${INSTALL_FROM}" == "xiaomi-fds" ]; then
        download_docker_images
    fi
    ${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" up -d
    ${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" ps
    print_success "Service started successfully, You can try access the service by clicking on the link below: "
    local ips=$(ip -4 addr show | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    for ip in $ips; do
        print_log " 🌐  https://${ip}:${BACKEND_PORT}"
    done
    return 0
}

# Update Service
update_service() {
    if ! is_service_installed; then
        print_error "Service not installed"
        return 0
    fi
    read -rp "[✳️ OPTION]  Update service will restart service, Are you sure to update service? (yes/No): "
    if [ "${REPLY}" != "yes" ]; then
        print_tip "Update cancelled"
        return 0
    fi
    print_log "Updating service..."
    if [ "${INSTALL_FROM}" == "xiaomi-fds" ]; then
        download_docker_images
    else
        ${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" pull
    fi
    ${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" down || true
    ${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" up -d
    ${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" ps
    local ips=$(ip -4 addr show | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    for ip in $ips; do
        print_log " 🌐  https://${ip}:${BACKEND_PORT}"
    done
    print_success "Service updated successfully!"
}

# Stop Service
stop_service() {
    if ! is_service_installed; then
        print_error "Service not installed"
        return 0
    fi
    if ! is_service_running; then
        print_error "Service not running"
        return 0
    fi
    read -rp "[✳️ OPTION]  Are you sure to stop service? (Yes/no): "
    if [ "${REPLY}" == "no" ]; then
        print_tip "Stop cancelled"
        return 0
    fi
    print_log "Stopping service..."
    ${DOCKER_CMD} compose -f "${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}" down
    print_success "Service stopped successfully: ${INSTALL_FULL_DIR}/${DOCKER_COMPOSE_FILE}"
}

check_service(){
    echo "check"
}

install_docker(){
    if [ "${DEPEND_DOCKER}" != "Unknown" ] && [ "${DEPEND_DOCKER_COMPOSE}" != "Unknown" ]; then
        print_success "Docker already installed"
        return 0
    fi
    if [[ ${WSL_VERSION} == WSL* ]]; then
        print_info "It is detected that you are now in a WSL environment. If you do not know how to \nconfigure the docker environment, you can ignore the following reminder. The script \nwill start to install automatically after 20 seconds."
    fi
    print_log "Please follow the prompts to install docker: "
    # Install Docker
    wget -qO- https://get.docker.com | bash -s docker --mirror "${MIRROR_GET_DOCKER}"
    # Add user to docker group
    if getent group docker > /dev/null 2>&1; then
        sudo usermod -aG docker $USER
        DOCKER_CMD="sudo docker"
        print_tip "Added user to docker group. You may need to log out and back in for changes to take effect."
    fi
    return 0
}

install_nvidia_env(){
    if [[ "${GPU_VENDOR}" != "NVIDIA" ]]; then
        print_error "NVIDIA runtime environment not supported, current gpu vendor: $GPU_VENDOR"
        return 1
    fi
    if [ "${DEPEND_NVIDIA_DRIVER}" != "Unknown" ] && [ "${DEPEND_NVIDIA_CUDA_TOOLKIT}" != "Unknown" ]; then
        print_success "NVIDIA runtime environment already installed"
        return 0
    fi
    print_log "Installing NVIDIA Driver and CUDA Toolkit..."
    
    case "${OS_DISTRO}" in
        ubuntu|debian)
            # ubuntu debian
            if ! install_nvidia_env_with_apt; then
                print_error "Install ${GPU_VENDOR} for ${OS_DISTRO} failed"
                return 1
            fi
        ;;
        amzn|fedora|kylin|rhel)
            # amazon-linux fedora kylinos oracle-linux rhel Rocky
            if ! install_nvidia_env_with_dnf; then
                print_error "Install ${GPU_VENDOR} for ${OS_DISTRO} failed"
                return 1
            fi
        ;;
        azl)
            # azure-linux
            if ! install_nvidia_env_with_tdnf; then
                print_error "Install ${GPU_VENDOR} for ${OS_DISTRO} failed"
                return 1
            fi
        ;;
        opensuse|sles)
            # opensuse sles
            if ! install_nvidia_env_with_zypper; then
                print_error "Install ${GPU_VENDOR} for ${OS_DISTRO} failed"
                return 1
            fi
        ;;
        *)
            print_error "Install ${GPU_VENDOR} failed, un-support os distro: ${OS_DISTRO}"
            return 1
        ;;
    esac
    return 0
}

install_nvidia_env_with_apt(){
    local cuda_keyring_version="1.1-1"
    local cuda_file_name="cuda-keyring_${cuda_keyring_version}_all.deb"
    local cuda_file_full_name="${INSTALL_FULL_DIR}/${cuda_file_name}"
    # TODO： perf logic, use cache dir
    if [ ! -d "${INSTALL_FULL_DIR}" ]; then
        cuda_file_full_name="/tmp/${cuda_file_name}"
    fi
    case "${WSL_VERSION}" in
        Unknown)
            local ver_major=$(echo "$OS_VERSION_ID" | cut -d. -f1)
            local ver_minor=$(echo "$OS_VERSION_ID" | cut -d. -f2)
            if [ "${OS_DISTRO}" == "debian" ]; then
                ver_minor=""
            fi
            local repo_url="https://developer.download.nvidia.com/compute/cuda/repos/${OS_DISTRO}${ver_major}${ver_minor}/${ARCH}/${cuda_file_name}"
            wget -c -O "${cuda_file_full_name}" "${repo_url}"
        ;;
        WSL2)
            wget -c -O "${cuda_file_full_name}" "https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/${ARCH}/${cuda_file_name}"
        ;;
        WSL1)
            print_error "${GPU_VENDOR} un-support WSL1 for ${OS_DISTRO}${ver_major}${ver_minor}"
            return 1
        ;;
    esac
    
    print_log "Downloaded: ${cuda_file_full_name}"
    sudo dpkg -i "${cuda_file_full_name}"
    # rm -rf "${cuda_file_full_name}"
    sudo apt-get update
    # TODO:
    # sudo apt-get -y install "cuda-toolkit-${NVIDIA_CUDA_TOOLKIT_VERSION}"
    if [ "${WSL_VERSION}" == "Unknown" ]; then
        sudo apt-get -y install cuda-drivers
    else
        print_log "Run in ${WSL_VERSION}, skip install NVIDIA Driver"
    fi
    # Update env
    # local rc_files=("${HOME}/.bashrc" "${HOME}/.zshrc")
    # for rc in "${rc_files[@]}"; do
    #     print_log "Check env file: ${rc}"
    #     if [ -f "${rc}" ]; then
    #         # Check PATH include /usr/local/cuda/bin
    #         if ! grep -q "/usr/local/cuda/bin" "${rc}"; then
    #             print_log "Update ${rc}, PATH append: /usr/local/cuda/bin"
    #             echo 'export PATH=/usr/local/cuda/bin:${PATH:-}' >> "${rc}"
    #         else
    #             print_log "PATH include /usr/local/cuda/bin, skip"
    #         fi
    #         # Check LD_LIBRARY_PATH include /usr/local/cuda/lib64
    #         if ! grep -q "/usr/local/cuda/lib64" "${rc}"; then
    #             print_log "Update ${rc} LD_LIBRARY_PATH append: /usr/local/cuda/lib64"
    #             echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}' >> "${rc}"
    #         else
    #             print_log "LD_LIBRARY_PATH include /usr/local/cuda/lib64, skip"
    #         fi
    #         source "${rc}"
    #     fi
    # done
    # export PATH="/usr/local/cuda/bin:${PATH:-}"
    # export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
    return 0
}

install_nvidia_env_with_dnf() {
    # Un-support WSL
    if [ "${WSL_VERSION}" != "Unknown" ]; then
        print_error "${GPU_VENDOR} un-support ${WSL_VERSION} for ${OS_DISTRO}${OS_VERSION_ID}"
        return 1
    fi
    local repo_url="https://developer.download.nvidia.com/compute/cuda/repos/${OS_DISTRO}${OS_VERSION_ID}/${ARCH}/cuda-${OS_DISTRO}${OS_VERSION_ID}.repo"
    sudo dnf config-manager --add-repo "${repo_url}"
    sudo dnf clean all
    # TODO:
    # sudo dnf -y install "cuda-toolkit-${NVIDIA_CUDA_TOOLKIT_VERSION}"
    if [ "${OS_DISTRO}" == "fedora" ]; then
        sudo dnf -y install cuda-drivers
    else
        sudo dnf -y module install nvidia-driver:latest-dkms
    fi
    return 0
}

install_nvidia_env_with_tdnf() {
    # Un-support WSL
    if [ "${WSL_VERSION}" != "Unknown" ]; then
        print_error "${GPU_VENDOR} un-support ${WSL_VERSION} for ${OS_DISTRO}${OS_VERSION_ID}"
        return 1
    fi
    local repo_url="https://developer.download.nvidia.com/compute/cuda/repos/${OS_DISTRO}${OS_VERSION_ID}/${ARCH}/cuda-${OS_DISTRO}${OS_VERSION_ID}.repo"
    curl "${repo_url}" | sudo tee "/etc/yum.repos.d/cuda-${OS_DISTRO}${OS_VERSION_ID}.repo"
    sudo tdnf -y install azurelinux-repos-extended
    sudo tdnf clean all
    # TODO:
    # sudo tdnf -y install "cuda-toolkit-${NVIDIA_CUDA_TOOLKIT_VERSION}"
    sudo tdnf -y install nvidia-open
    return 0
}

install_nvidia_env_with_zypper() {
    # Un-support WSL
    if [ "${WSL_VERSION}" != "Unknown" ]; then
        print_error "${GPU_VENDOR} un-support ${WSL_VERSION} for ${OS_DISTRO}${OS_VERSION_ID}"
        return 1
    fi
    local repo_url="https://developer.download.nvidia.com/compute/cuda/repos/${OS_DISTRO}${OS_VERSION_ID}/${ARCH}/cuda-${OS_DISTRO}${OS_VERSION_ID}.repo"
    sudo zypper addrepo "${repo_url}"
    sudo zypper refresh
    # TODO:
    # sudo zypper install -y "cuda-toolkit-${NVIDIA_CUDA_TOOLKIT_VERSION}"
    sudo zypper install -y cuda-drivers
    return 0
}


install_nvidia_container_env(){
    if [[ "${GPU_VENDOR}" != "NVIDIA" ]]; then
        print_error "NVIDIA runtime environment not supported, current gpu vendor: $GPU_VENDOR"
        return 1
    fi
    
    if [[ "${DEPEND_NVIDIA_CONTAINER_TOOLKIT}" != "Unknown" ]]; then
        print_success "NVIDIA container toolkit already installed"
        return 0
    fi
    
    print_log "Installing NVIDIA Container Toolkit..."
    
    local NVIDIA_CONTAINER_TOOLKIT_VERSION=1.18.0-1
    
    case "${OS_DISTRO}" in
        ubuntu|debian)
            # With apt
            sudo apt-get update && sudo apt-get install -y --no-install-recommends curl gnupg2
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
            && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
            sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
            sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
            sudo sed -i -e '/experimental/ s/^#//g' /etc/apt/sources.list.d/nvidia-container-toolkit.list
            sudo apt-get update
            sudo apt-get install -y \
            nvidia-container-toolkit=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            nvidia-container-toolkit-base=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            libnvidia-container-tools=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            libnvidia-container1=${NVIDIA_CONTAINER_TOOLKIT_VERSION}
        ;;
        amzn|fedora|kylin|rhel)
            # With dnf
            sudo dnf install -y curl
            curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | \
            sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo
            sudo dnf-config-manager --enable nvidia-container-toolkit-experimental
            sudo dnf install -y \
            nvidia-container-toolkit-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            nvidia-container-toolkit-base-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            libnvidia-container-tools-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            libnvidia-container1-${NVIDIA_CONTAINER_TOOLKIT_VERSION}
        ;;
        opensuse|sles)
            # With zypper
            sudo zypper ar https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo
            sudo zypper modifyrepo --enable nvidia-container-toolkit-experimental
            sudo zypper --gpg-auto-import-keys install -y \
            nvidia-container-toolkit-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            nvidia-container-toolkit-base-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            libnvidia-container-tools-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
            libnvidia-container1-${NVIDIA_CONTAINER_TOOLKIT_VERSION}
        ;;
        *)
            print_error "${GPU_VENDOR} un-support OS distro: ${OS_DISTRO}${OS_VERSION_ID}"
            return
        ;;
    esac
    # Configure Docker，
    print_log "Configuring Docker..."
    # Modify /etc/docker/daemon.json
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
    return 0
}

install_amd_env(){
    if [[ "${GPU_VENDOR}" != "AMD" ]]; then
        print_error "AMD runtime environment not supported, current gpu vendor: $GPU_VENDOR"
        return 1
    fi
    print_log "TODO: Installing AMD Runtime Environment..."
    
    return 0
}

# Install Runtime Environment
install_runtime_environment() {
    # print_header
    print_log "Installing runtime environment..."
    
    get_system_info
    get_runtime_environment
    
    # Install based on OS
    if [[ "${OS}" == "Linux" ]]; then
        print_log "Installing on ${OS}..."
        # Install Docker
        if ! install_docker; then
            print_error "Docker installation failed!"
            return 1
        fi
        if [ "${INSTALL_MODE}" != "lite" ]; then
            if [[ "$GPU_VENDOR" == "NVIDIA" ]]; then
                if ! install_nvidia_env; then
                    print_error "NVIDIA runtime environment installation failed!"
                    return 1
                fi
                if ! install_nvidia_container_env; then
                    print_error "NVIDIA container toolkit installation failed!"
                    return 1
                fi
                elif [[ "$GPU_VENDOR" == "AMD" ]]; then
                if ! install_amd_env; then
                    print_error "AMD runtime environment installation failed!"
                    return 1
                fi
            fi
        else
            print_log "Install without GPU support, skipping GPU env installation"
        fi
        elif [[ "$OS" == "macOS" ]]; then
        print_warning "Please install Docker Desktop for Mac manually from https://www.docker.com/products/docker-desktop"
        return 1
    else
        print_error "Unsupported operating system: ${OS}"
        return 1
    fi
    
    print_success "Runtime environment installation completed!"
    return 0
}

# Uninstall
uninstall() {
    print_log "Start uninstalling service..."
    check_root
    
    if ! is_service_installed; then
        print_error "Service is not installed"
        return 0
    fi
    
    print_warning "Uninstall service will stop service and he following folders will be ${RED}deleted${NC}:"
    print_warning "- Installation directory : ${YELLOW}${INSTALL_FULL_DIR}${NC}"
    print_warning "- Configuration directory: ${YELLOW}${PROJECT_HOME_DIR}${NC}"
    read -rp "[✳️ OPTION]  Are you sure to uninstall the service? [yes/No]: "
    if [ "${REPLY}" != "yes" ]; then
        print_tip "Uninstallation cancelled."
        return 0
    fi
    
    stop_service
    
    print_log "Removing service configuration file..."
    sudo rm -rf "${PROJECT_HOME_DIR}"
    
    print_log "Removing service installation directory..."
    sudo rm -rf "${INSTALL_FULL_DIR}"
    
    print_success "Service uninstalled successfully!"
}

# Show Help
show_help() {
    # print_header
    print_log "Available commands:"
    print_log "  quick_install                 - Perform quick installation"
    print_log "  quick_install_lite            - Perform quick installation (without AI Engine)"
    print_log "  start_service                 - Start the service"
    print_log "  update_service                - Update the service"
    print_log "  stop_service                  - Stop the service"
    print_log "  install_runtime_env           - Install runtime environment"
    print_log "  check_runtime_env             - Check runtime environment"
    print_log "  uninstall                     - Uninstall the service"
    print_log "  help                          - Show this help message"
    print_log "  exit                          - Exit the script"
    print_log ""
    print_log "Usage:"
    print_log "  Run this script and select options from the menu"
    print_log "  or execute directly with a command: ./install.sh <command>"
}

# Main interactive menu
show_menu() {
    while true; do
        print_header
        print_log "Main Menu:"
        print_log "1.  Quick Install"
        print_log "2.  Quick Install (Without AI Engine)"
        print_log "3.  Start Service"
        print_log "4.  Update Service"
        print_log "5.  Stop Service"
        print_log "6.  Install Runtime Environment"
        print_log "7.  Check Runtime Environment"
        print_log "8.  Uninstall"
        print_log "9.  Help"
        print_log "q.  Exit"
        print_log ""
        read -p "[✳️ INPUT] Select an option (1-9): " choice
        case $choice in
            1) quick_install ;;
            2) quick_install_lite ;;
            3) start_service ;;
            4) update_service ;;
            5) stop_service ;;
            6)
                get_system_info
                get_runtime_environment
                print_system_info
                print_runtime_environment
                install_runtime_environment
            ;;
            7)
                get_system_info
                get_runtime_environment
                print_system_info
                print_runtime_environment
                print_service_status
            ;;
            8) uninstall ;;
            9) show_help ;;
            q)
                print_log "Exiting..."
                exit 0
            ;;
            *)
                print_error "Invalid option. Please select 1-9."
            ;;
        esac
        
        print_log ""
        read -p "[✳️ INPUT] Press Enter to continue..."
    done
}

# Main function
main() {
    check_docker
    # If argument provided, execute that command directly
    if [ $# -gt 0 ]; then
        # Update docker cmd
        case "$1" in
            quick_install) quick_install ;;
            quick_install_lite) quick_install_lite ;;
            start_service) start_service ;;
            update_service) update_service ;;
            stop_service) stop_service ;;
            install_runtime_env)
                get_system_info
                get_runtime_environment
                print_system_info
                print_runtime_environment
                install_runtime_environment
            ;;
            check_runtime_env)
                get_system_info
                get_runtime_environment
                print_system_info
                print_runtime_environment
            ;;
            uninstall) uninstall ;;
            help) show_help ;;
            exit) exit 0 ;;
            *)
                print_log "Unknown command: $1";
                show_help
            ;;
        esac
        exit 0
    fi
    
    # Otherwise show interactive menu
    show_menu
}

# Run main function with all arguments
main "$@"
