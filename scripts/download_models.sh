#!/bin/bash
#
# Model Download Script V2
# Support downloading models from ModelScope or HuggingFace
#
set +e

# Default model list (model_id:filename format)
DEFAULT_MODELS=(
    "xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF:mmproj-MiMo-VL-Miloco-7B_BF16.gguf"
    "xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF:MiMo-VL-Miloco-7B_Q4_0.gguf"
    "Qwen/Qwen3-8B-GGUF:Qwen3-8B-Q4_K_M.gguf"
)

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_REL_PATH="$SCRIPT_DIR/../models"
DEFAULT_TARGET_PATH="$(cd "$(dirname "$MODELS_REL_PATH")" 2>/dev/null && pwd)/$(basename "$MODELS_REL_PATH")"
DEFAULT_DOWNLOAD_SOURCE="modelscope"

# Global variables
DOWNLOAD_SOURCE="$DEFAULT_DOWNLOAD_SOURCE"
TARGET_DIR="$DEFAULT_TARGET_PATH"
AUTO_INSTALL="true"
MODEL_ENTRIES=()

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored message
print_info() {
    echo -e "${BLUE}$1${NC}"
}

print_success() {
    echo -e "${GREEN}$1${NC}"
}

print_warning() {
    echo -e "${YELLOW}$1${NC}"
}

print_error() {
    echo -e "${RED}$1${NC}"
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Find download tool (wget or curl)
find_download_tool() {
    if command_exists wget; then
        echo "wget"
    elif command_exists curl; then
        echo "curl"
    else
        echo ""
    fi
}

# Calculate file SHA256 hash
calculate_file_sha256() {
    local file="$1"
    
    if [ ! -f "$file" ]; then
        return 1
    fi
    
    if command_exists sha256sum; then
        sha256sum "$file" | cut -d' ' -f1
    elif command_exists shasum; then
        shasum -a 256 "$file" | cut -d' ' -f1
    else
        return 1
    fi
}

# Get remote file SHA256 hash from HuggingFace API
get_huggingface_file_sha256() {
    local model_id="$1"
    local filename="$2"
    local tool=$(find_download_tool)
    
    local api_url="https://huggingface.co/api/models/$model_id/tree/main"
    local api_response=""

    if [ "$tool" = "curl" ]; then
        api_response=$(curl -s "$api_url" 2>/dev/null || echo "")
    else
        api_response=$(wget -qO- "$api_url" 2>/dev/null || echo "")
    fi
    if [ -z "$api_response" ]; then
        return 1
    fi
    # Extract SHA256 from HuggingFace API response
    # API format: [{"type":"file",...,"lfs":{"oid":"sha256_hash",...},...,"path":"filename"},...]
    # Convert to single line for easier processing
    local single_line=$(echo "$api_response" | tr '\n' ' ')
    
    local objects=$(echo "$single_line" | sed 's/\[{/{/g; s/}\]/}/g' | sed 's/},{/}\n{/g')
    
    local sha256_hash=""
    while IFS= read -r obj; do
        # Check if this object contains the matching path (case-insensitive)
        if echo "$obj" | grep -qiE '"path":"'$filename'"'; then
            # Extract lfs.oid from this object
            sha256_hash=$(echo "$obj" | grep -ioE '"lfs":\{[^}]*"oid":"[a-f0-9]{64}"' | grep -ioE '"oid":"[a-f0-9]{64}"' | cut -d'"' -f4 | head -1 || echo "")
            if [ -n "$sha256_hash" ]; then
                break
            fi
        fi
    done <<< "$objects"

    # Only accept SHA256 (64 chars)
    if [ -n "$sha256_hash" ] && [ ${#sha256_hash} -eq 64 ]; then
        echo "$sha256_hash"
        return 0
    fi
    
    return 1
}

# Get remote file SHA256 hash from ModelScope API
get_modelscope_file_sha256() {
    local model_id="$1"
    local filename="$2"
    local tool=$(find_download_tool)
    
    local api_url="https://www.modelscope.cn/api/v1/models/$model_id/repo/files"
    local api_response=""
    
    if [ "$tool" = "curl" ]; then
        api_response=$(curl -s "$api_url" 2>/dev/null || echo "")
    else
        api_response=$(wget -qO- "$api_url" 2>/dev/null || echo "")
    fi
    
    if [ -z "$api_response" ]; then
        return 1
    fi
    
    local sha256_hash=$(echo "$api_response" | grep -oE "\"Path\":\"$filename\"[^}]*\"Sha256\":\"[a-f0-9]{64}\"" | grep -oE '"Sha256":"[a-f0-9]{64}"' | cut -d'"' -f4 || echo "")
    
    # Alternative: try to find Sha256 near the Path
    if [ -z "$sha256_hash" ]; then
        # Look for the pattern: "Path":"filename" ... "Sha256":"hash"
        sha256_hash=$(echo "$api_response" | grep -A 20 "\"Path\":\"$filename\"" | grep -oE '"Sha256":"[a-f0-9]{64}"' | head -1 | cut -d'"' -f4 || echo "")
    fi
    
    # Only accept SHA256 (64 chars)
    if [ -n "$sha256_hash" ] && [ ${#sha256_hash} -eq 64 ]; then
        echo "$sha256_hash"
        return 0
    fi
    
    return 1
}

# Check file integrity with SHA256
check_file_integrity() {
    local file="$1"
    local model_id="$2"
    local filename="$3"
    local source="$4"
    
    if [ ! -f "$file" ]; then
        return 2  # File doesn't exist
    fi
    
    print_info "   Checking file integrity (SHA256)..."
    
    # Get remote SHA256 hash
    local remote_sha256=""
    if [ "$source" = "huggingface" ]; then
        remote_sha256=$(get_huggingface_file_sha256 "$model_id" "$filename")
    elif [ "$source" = "modelscope" ]; then
        remote_sha256=$(get_modelscope_file_sha256 "$model_id" "$filename")
    fi
    
    if [ -z "$remote_sha256" ]; then
        print_warning "   ⚠️  Could not verify remote file SHA256 (API may not support it)"
        return 3  # Cannot verify
    fi
    
    # Calculate local file SHA256
    local local_sha256=$(calculate_file_sha256 "$file")
    if [ -z "$local_sha256" ]; then
        print_warning "   ⚠️  Could not calculate local file SHA256"
        return 3
    fi
    
    # Compare SHA256 hashes (case-insensitive)
    local remote_sha256_lower=$(echo "$remote_sha256" | tr '[:upper:]' '[:lower:]')
    local local_sha256_lower=$(echo "$local_sha256" | tr '[:upper:]' '[:lower:]')
    
    if [ "$remote_sha256_lower" = "$local_sha256_lower" ]; then
        print_success "   ✅ File integrity verified (SHA256 matches)"
        return 0  # SHA256 matches
    else
        print_error "   ❌ File integrity check failed!"
        print_warning "      Local SHA256:  $local_sha256_lower"
        print_warning "      Remote SHA256: $remote_sha256_lower"
        return 1  # SHA256 mismatch
    fi
}

# Check download environment
check_environment() {
    print_info "🔍 Checking download environment..."
    
    local tool=$(find_download_tool)
    
    if [ -z "$tool" ]; then
        print_error "❌ No download tool found (wget or curl)"
        if [ "$AUTO_INSTALL" = "true" ]; then
            print_warning "⚠️  Attempting to install wget..."
            if command_exists apt-get; then
                sudo apt-get update && sudo apt-get install -y wget
            elif command_exists yum; then
                sudo yum install -y wget
            elif command_exists brew; then
                brew install wget
            else
                print_error "❌ Cannot auto-install wget. Please install wget or curl manually."
                return 1
            fi
            
            tool=$(find_download_tool)
            if [ -z "$tool" ]; then
                print_error "❌ Still no download tool available after installation attempt"
                return 1
            fi
        else
            print_warning "📝 Please install wget or curl manually"
            return 1
        fi
    fi
    
    print_success "✅ Download tool ready: $tool"
    return 0
}

# Download file using wget or curl with resume support and retry logic
download_file() {
    local url="$1"
    local output="$2"
    local model_id="$3"
    local filename="$4"
    local source="$5"
    
    local max_retries=1
    local retry_count=0
    
    while [ $retry_count -le $max_retries ]; do
        local tool=$(find_download_tool)
        
        if [ -z "$tool" ]; then
            print_error "❌ No download tool available"
            return 1
        fi
        
        local dir=$(dirname "$output")
        mkdir -p "$dir"
        
        # Download the file with resume support (-c for wget, -C - for curl)
        if [ $retry_count -eq 0 ]; then
            print_info "   Downloading: $filename"
        else
            print_info "   Retrying download (attempt $((retry_count + 1))/$((max_retries + 1))): $filename"
        fi
        
        local ret=0
        if [ "$tool" = "wget" ]; then
            # wget progress bar
            wget -c --progress=bar:force -O "$output" "$url" 2>&1
            ret=$?
        else
            # curl progress bar
            curl -L -C - --progress-bar -o "$output" "$url" 2>&1
            ret=$?
            # Add newline after curl progress (curl doesn't add one)
            echo ""
        fi
        
        # Check if download was successful and file exists and is not empty
        if [ $ret -eq 0 ] && [ -f "$output" ] && [ -s "$output" ]; then
            # Verify downloaded file
            print_info "   Verifying downloaded file..."
            check_file_integrity "$output" "$model_id" "$filename" "$source"
            local verify_result=$?
            if [ $verify_result -eq 0 ]; then
                print_success "   ✅ Downloaded file verified successfully"
                return 0
            elif [ $verify_result -eq 1 ]; then
                print_error "   ❌ Downloaded file verification failed!"
                print_warning "   The downloaded file SHA256 does not match the remote file."
                echo ""
                if [ -t 0 ]; then
                    # Interactive mode
                    read -p "   Delete and re-download? [Y/n]: " -n 1 -r
                    echo
                    if [[ $REPLY =~ ^[Nn]$ ]]; then
                        print_warning "   Keeping corrupted file: $filename"
                        return 0
                    else
                        print_info "   Deleting corrupted file and will retry..."
                        rm -f "$output"
                        retry_count=$((retry_count + 1))
                        if [ $retry_count -le $max_retries ]; then
                            continue
                        else
                            print_error "   ❌ Download failed after $((max_retries + 1)) attempts"
                            return 1
                        fi
                    fi
                else
                    # Non-interactive mode, auto-delete and retry
                    print_info "   Auto-deleting corrupted file (non-interactive mode)..."
                    rm -f "$output"
                    retry_count=$((retry_count + 1))
                    if [ $retry_count -le $max_retries ]; then
                        continue
                    else
                        print_error "   ❌ Download failed after $((max_retries + 1)) attempts"
                        return 1
                    fi
                fi
            else
                # Can't verify, but file was downloaded
                print_warning "   ⚠️  Could not verify downloaded file, but download completed"
                return 0
            fi
        else
            # Clean up failed download
            [ -f "$output" ] && rm -f "$output"
            return 1
        fi
    done
    
    return 1
}

# Download from HuggingFace
download_from_huggingface() {
    local model_id="$1"
    local filename="$2"
    
    print_info "\n📦 Downloading from HuggingFace: $model_id"
    print_info "   File: $filename"
    
    local local_dir="$TARGET_DIR/$model_id"
    local url="https://huggingface.co/$model_id/resolve/main/$filename"
    local output="$local_dir/$filename"
    
    if download_file "$url" "$output" "$model_id" "$filename" "huggingface"; then
        print_success "✅ Download completed: $output"
        return 0
    else
        print_error "❌ HuggingFace download failed"
        return 1
    fi
}

# Download from ModelScope
download_from_modelscope() {
    local model_id="$1"
    local filename="$2"
    
    print_info "\n📦 Downloading from ModelScope: $model_id"
    print_info "   File: $filename"
    
    local local_dir="$TARGET_DIR/$model_id"
    local url="https://modelscope.cn/$model_id/resolve/main/$filename"
    local output="$local_dir/$filename"
    
    if download_file "$url" "$output" "$model_id" "$filename" "modelscope"; then
        print_success "✅ Download completed: $output"
        return 0
    else
        print_error "❌ ModelScope download failed"
        return 1
    fi
}

# Download a single model
download_model() {
    local model_entry="$1"
    
    # Parse model_id:filename
    IFS=':' read -r model_id filename <<< "$model_entry"
    
    # Validate format
    if [ -z "$model_id" ] || [ -z "$filename" ]; then
        print_error "❌ Invalid model entry format: $model_entry"
        print_error "   Expected format: model_id:filename"
        return 1
    fi
    
    if [ "$DOWNLOAD_SOURCE" = "modelscope" ]; then
        download_from_modelscope "$model_id" "$filename"
    elif [ "$DOWNLOAD_SOURCE" = "huggingface" ]; then
        download_from_huggingface "$model_id" "$filename"
    else
        print_error "❌ Unsupported download source: $DOWNLOAD_SOURCE"
        return 1
    fi
}

# Download multiple models
download_models() {
    local model_count=${#MODEL_ENTRIES[@]}
    
    print_info "\n🎯 Preparing to download $model_count model(s)"
    echo "============================================================"
    
    local success_count=0
    local idx=1
    
    for model_entry in "${MODEL_ENTRIES[@]}"; do
        print_info "\n[$idx/$model_count] Processing: $model_entry"
        
        if download_model "$model_entry"; then
            success_count=$((success_count + 1))
        fi
        
        idx=$((idx + 1))
    done
    
    echo ""
    echo "============================================================"
    print_info "📊 Download completed: $success_count/$model_count succeeded"
    
    if [ $success_count -eq $model_count ]; then
        return 0
    else
        return 1
    fi
}

# Show usage
show_usage() {
    cat << EOF
Model Download Script V2 - Support downloading models from ModelScope or HuggingFace

Usage: $0 [OPTIONS] [MODEL_ENTRIES...]

Options:
  --source SOURCE          Download source: modelscope or huggingface (default: modelscope)
  --target DIR             Download target directory (default: project_root/models)
  --auto-install BOOL      Auto-install missing dependencies(wget) (default: true)
  -h, --help               Show this help message

Model Entries:
  Each model entry must be in format: model_id:filename
  Example: xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF:MiMo-VL-Miloco-7B_Q4_0.gguf

  If no model entries are provided, default models will be used.

Examples:
  # Download default models (from ModelScope)
  $0
  
  # Download default models from HuggingFace
  $0 --source huggingface
  
  # Download specific model
  $0 xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF:MiMo-VL-Miloco-7B_Q4_0.gguf
  
  # Download multiple models
  $0 xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF:mmproj-MiMo-VL-Miloco-7B_BF16.gguf \
  xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF:MiMo-VL-Miloco-7B_Q4_0.gguf
  
  # Specify download directory
  $0 --target /path/to/models xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF:MiMo-VL-Miloco-7B_Q4_0.gguf
  
  # Disable auto-install dependencies
  $0 --auto-install false xiaomi-open-source/Xiaomi-MiMo-VL-Miloco-7B-GGUF:MiMo-VL-Miloco-7B_Q4_0.gguf

EOF
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --source)
                DOWNLOAD_SOURCE="$2"
                shift 2
                ;;
            --target)
                TARGET_DIR="$2"
                shift 2
                ;;
            --auto-install)
                AUTO_INSTALL="$2"
                shift 2
                ;;
            -h|--help)
                show_usage
                exit 0
                ;;
            -*)
                print_error "Unknown option: $1"
                show_usage
                exit 1
                ;;
            *)
                # Model entry in format model_id:filename
                if [[ "$1" == *":"* ]]; then
                    MODEL_ENTRIES+=("$1")
                else
                    print_error "Invalid model entry format: $1"
                    print_error "Expected format: model_id:filename"
                    exit 1
                fi
                shift
                ;;
        esac
    done
}

# Main function
main() {
    # Parse arguments
    parse_args "$@"
    
    # Convert to absolute path
    if [ ! -d "$TARGET_DIR" ]; then
        mkdir -p "$TARGET_DIR"
    fi
    TARGET_DIR=$(cd "$TARGET_DIR" 2>/dev/null && pwd || echo "$(pwd)/$TARGET_DIR")
    if [ ! -d "$TARGET_DIR" ]; then
        print_error "❌ Cannot access target directory: $TARGET_DIR"
        exit 1
    fi
    
    # Normalize source name
    DOWNLOAD_SOURCE=$(echo "$DOWNLOAD_SOURCE" | tr '[:upper:]' '[:lower:]')
    
    # Use default models if none specified
    if [ ${#MODEL_ENTRIES[@]} -eq 0 ]; then
        print_info "No model entries specified, using default models..."
        MODEL_ENTRIES=("${DEFAULT_MODELS[@]}")
    fi
    
    # Validate all model entries have filename
    for entry in "${MODEL_ENTRIES[@]}"; do
        if [[ "$entry" != *":"* ]]; then
            print_error "❌ Invalid model entry format: $entry"
            print_error "   Expected format: model_id:filename (filename is required)"
            exit 1
        fi
        IFS=':' read -r model_id filename <<< "$entry"
        if [ -z "$filename" ]; then
            print_error "❌ Missing filename in model entry: $entry"
            print_error "   Expected format: model_id:filename (filename is required)"
            exit 1
        fi
    done
    
    # Print configuration
    print_info "📁 Download directory: $TARGET_DIR"
    print_info "🌐 Download source: $DOWNLOAD_SOURCE"
    echo "------------------------------------------------------------"
    
    # Check environment
    if ! check_environment; then
        exit 1
    fi
    
    echo ""
    
    # Download models
    if download_models; then
        echo ""
        print_success "🎉 All models downloaded successfully!"
        exit 0
    else
        echo ""
        print_warning "⚠️  Some models failed to download"
        exit 1
    fi
}

# Run main function
main "$@"

