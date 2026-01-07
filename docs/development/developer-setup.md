# Local Development Guide

## Frontend Development Documentation

### Environment Requirements

- Node.js >= 20.0.0
- npm >= 9.0.0

### Quick Start

#### 1. Install Dependencies

```bash
cd web_ui

npm install
```

**Note**: If you don't want to start the frontend service and only need to develop the `server` service, you can skip directly to step 5 to build the frontend artifacts for the `server` to use.

#### 2. Generate SSL Certificates

The development environment uses HTTPS, so you need to generate local certificates first:

```bash
# If the `certs` directory doesn't exist, create it first
mkdir certs

# Enter the certs directory
cd certs

# Generate private key
openssl genrsa -out localhost-key.pem 2048

# Generate certificate
openssl req -new -x509 -key localhost-key.pem -out localhost.pem -days 365 -subj "/CN=localhost"

# Return to project root directory
cd ..
```

#### 3. Configure Backend Proxy

Edit the `./web_ui/config.js` file to modify the backend API address:

```javascript
export const config = {
  api: {
    target: 'https://127.0.0.1:8000/',  // Modify to your backend IP address and port
  },
  // ...
}
```

**Description**:
- The `target` field configures the complete address of the backend service (including protocol, IP, and port)
- The frontend will proxy to the backend service through the `/api` path
- No restart is needed after modification, Vite will automatically hot reload

#### 4. Start Frontend

```bash
npm run dev
```

After successful startup, access:
- HTTPS: `https://127.0.0.1:5173`

#### 5. Build Frontend

```bash
npm run build
```

After the build is complete, the build artifacts will be output to the `dist` directory.

**Note**: If you don't want to start the frontend service, you can copy all files from the `dist` directory to `miloco_server/static/` for use. See the backend development steps below.

## Backend Development

### Environment Requirements
- Python: 3.12.x

### Quick Start
The backend can be developed independently without starting the AI engine, and can be configured to use cloud models.

#### 1. Install miot_kit
```bash
# Enter from project root directory
cd miot_kit

# Install
pip install -e .

# Return to root directory
cd ..
```

#### 2. Install Backend
```bash
# Enter from project root directory
cd miloco_server

# Install
pip install -e .

# Return to root directory
cd ..
```

#### 3. Copy Frontend Build Artifacts
Refer to the frontend development steps, copy all files from the frontend build `dist` directory to `miloco_server/static/`.

That is:
```plaintext
miloco_server
└── static
    ├── assets
    └── index.html
```

#### 4. Start
```bash
# Under project root directory
python scripts/start_server.py
```

After the service starts, you can access the API documentation at: `https://<your-ip>:8000/docs`

## AI Engine Development

```bash

# Install dependencies
pip install -e miloco_ai_engine

# Build core (choose one)
# - Linux/WSL2 with NVIDIA GPU: use CUDA build
# - macOS (Apple Silicon): use MPS build (device: "mps" in config)
bash scripts/ai_engine_cuda_build.sh
# bash scripts/ai_engine_metal_build.sh

# Configure dynamic library path
export LD_LIBRARY_PATH=project_root/output/lib:$LD_LIBRARY_PATH
# macOS: export DYLD_LIBRARY_PATH=project_root/output/lib:$DYLD_LIBRARY_PATH

# Run service
python scripts/start_ai_engine.py

```
  
After the service starts, you can access the API documentation at: `https://<your-ip>:8001/docs`

# Project Configuration

Configure service behavior before startup by modifying configuration files, which can be customized.
  
## Backend Service Configuration:
- [Frontend Service Proxy Configuration](../../web_ui/config.js)
- [Backend Service Configuration](../../config/server_config.yaml)
- [Prompt Configuration](../../config/prompt_config.yaml)

## AI Engine Configuration:
- [AI Engine Service Configuration](../../config/ai_engine_config.yaml)
