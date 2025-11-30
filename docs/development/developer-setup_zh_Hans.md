# 本地开发指南

## 前端开发文档

### 环境要求

- Node.js >= 20.0.0
- npm >= 9.0.0

### 快速开始

#### 1. 安装依赖

```bash
cd web_ui

npm install
```

**注意：如果不想启动前端服务，只需要开发`server`服务，可以直接跳到第五步打包生成前端产物供`server`使用**

#### 2. 生成启动证书

开发环境使用 HTTPS，需要先生成本地证书：

```bash
# 如果 `certs` 目录不存在，请先创建
mkdir certs

# 进入 certs 目录
cd certs

# 生成私钥
openssl genrsa -out localhost-key.pem 2048

# 生成证书
openssl req -new -x509 -key localhost-key.pem -out localhost.pem -days 365 -subj "/CN=localhost"

# 返回项目根目录
cd ..
```

#### 3. 配置后端代理

编辑 `./web_ui/config.js` 文件，修改后端 API 地址：

```javascript
export const config = {
  api: {
    target: 'https://127.0.0.1:8000/',  // 修改为你的后端 IP 地址和端口
  },
  // ...
}
```

**说明**：
- `target` 字段配置后端服务的完整地址（包含协议、IP 和端口）
- 前端会通过 `/api` 路径代理到后端服务
- 修改后无需重启，Vite 会自动热更新

#### 4. 前端启动

```bash
npm run dev
```

启动成功后，访问：
- HTTPS: `https://127.0.0.1:5173`

#### 5. 前端打包

```bash
npm run build
```

打包完成后，构建产物会输出到 `dist` 目录。

**注意**：如果不想启动前端服务，可以将上述的`dist`里的所有文件复制到`miloco_server/static/`里使用。可查看下述后端开发步骤。

## 后端开发
### 环境要求
- python：3.12.x

### 快速开始
后端可以独自开发，不依赖AI引擎启动，可以配置云端模型使用。

#### 1. 安装miot_kit
```bash
# 从根目录项目进入
cd miot_kit

# 安装
pip install -e .

# 回到根目录
cd ..
```
#### 2. 安装后端
```bash
# 从根目录项目进入
cd miloco_server

# 安装
pip install -e .

# 回到根目录
cd ..
```
#### 3. 复制前端打包产物
参考前端开发步骤，将前端打包的`dist`里的所有文件复制到`miloco_server/static/`里。

即：
```plaintext
miloco_server
└── static
    ├── assets
    └── index.html
```

#### 4. 启动
```bash
# 项目根目录下
python scripts/start_server.py
```

服务启动后，可以通过地址访问API文档：`https://<your-ip>:8000/docs`

## AI引擎开发

```bash

# 安装依赖
pip install -e miloco_ai_engine

# 编译 core（按环境选择）
# - Linux/WSL2 + NVIDIA GPU: 使用 CUDA 构建
# - macOS（Apple Silicon）: 使用 MPS 构建（配置里 device: "mps"）
bash scripts/ai_engine_cuda_build.sh
# bash scripts/ai_engine_metal_build.sh

# 配置动态库路径
export LD_LIBRARY_PATH=project_root/output/lib:$LD_LIBRARY_PATH
# macOS: export DYLD_LIBRARY_PATH=project_root/output/lib:$DYLD_LIBRARY_PATH

# 运行服务
python scripts/start_ai_engine.py

```
  
服务启动后，可以通过地址访问API文档：`https://<your-ip>:8001/docs`

# 项目配置

通过修改配置文件在启动前配置服务的行为，可以自定义。
  
## 后端服务配置:
- [前端服务代理配置](../../web_ui/config.js)
- [后端服务配置](../../config/server_config.yaml)
- [prompt配置](../../config/prompt_config.yaml)

## AI引擎配置:
- [AI引擎服务配置](../../config/ai_engine_config.yaml)

  
