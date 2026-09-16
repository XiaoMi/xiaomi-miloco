#!/usr/bin/env bash
# Miloco 独立 macOS 应用构建脚本（slim edition / Apple Silicon）
#
# 产物：
#   dist/app/Miloco.app                      内嵌 CPython + 精简依赖 + web 页面 + 米家相机原生库
#   dist/app/Miloco-<ver>-arm64.dmg          （--dmg）
#
# 与 scripts/build.sh 的关系：**刻意不复用**——build.sh 会 rm -rf dist/ 并把 ONNX 模型、
# CLI、openclaw/hermes 插件、supervisor 一起打包，那些正是 App 要去掉的东西。只复用
# scripts/version_normalize.py 做 CalVer ↔ PEP440 换算，行为与发布流程保持一致。
#
# 用法：
#   app/build_app.sh                     # 完整构建 + 冒烟测试
#   app/build_app.sh --skip-web          # 跳过前端构建（前端未改时提速）
#   app/build_app.sh --no-smoke          # 跳过冒烟测试
#   app/build_app.sh --dmg               # 额外产出 dmg
#   app/build_app.sh --version 2026.7.3  # 显式指定版本（默认从 git 推导）

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT/app"
OUT_DIR="$ROOT/dist/app"
APP="$OUT_DIR/Miloco.app"
BUNDLE_ID="com.xiaomi.miloco"
PY_VERSION="${MILOCO_APP_PY_VERSION:-3.12}"
TARGET_MACOS="14.0"   # av/opencv 的 arm64 wheel 最低要求（av: macosx_14_0）

VERSION=""
DO_WEB=1
DO_SMOKE=1
DO_DMG=0
DO_SIGN=1

# slim edition 保留的顶层依赖 = 两个 wheel 的运行依赖 **减去** 重依赖。
# 删掉的是：onnxruntime / scipy / tokenizers（全量感知才需要）、
# fastmcp（仅 miot/mcp.py 使用，全仓库无人 import）、zeroconf（仅 miot/mdns.py，
# mDNS 是死桩，局域网发现实际走 lan.py 的 UDP 广播）。
# 注意 pydantic-settings / sse-starlette / pyyaml / cryptography 必须显式列上：
# 它们过去靠 miloco-miot → fastmcp → mcp 传递进来，断了 fastmcp 就会 ImportError。
SLIM_DEPS=(
  "aiofiles>=25.1.0"
  "apscheduler>=3.10,<4"
  "av>=17.1.0"
  "fastapi>=0.136.3"
  "httpx>=0.28.1"
  "numpy>=2.2.0"
  "opencv-python-headless>=4.13.0.92"
  "Pillow>=12.2.0"
  "psutil>=7.2.2"
  "pydantic>=2.13.4"
  "python-dotenv>=1.2.2"
  "uvicorn[standard]>=0.49.0"
  # pi-heif 只有 2.6MB，但 identity/_image_utils 在 slim 启动链上仍会被 import（HEIC 解码
  # 注册器）；不装它每次启动都会往日志里打一条 heif_decoder_unavailable + traceback。
  "pi-heif>=1.4.0"
  "pydantic-settings>=2.0"
  "sse-starlette>=3.0"
  "pyyaml>=6.0"
  "aiocache>=0.12.3"
  "aiohttp>=3.14.1"
  "cryptography>=42.0"
  "paho-mqtt>=2.1.0"
)

# 这些包绝不允许出现在 App 里（出现即构建失败）。
BANNED_PACKAGES=(onnxruntime scipy tokenizers fastmcp zeroconf mcp)

# ─── 输出 ───────────────────────────────────────────────────────────────────

log() { printf '\033[36m[app]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[app][warn]\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[31m[app][error]\033[0m %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

# ─── 参数 ───────────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:-}"
      [[ -n "$VERSION" ]] || die "--version 需要参数"
      shift 2
      ;;
    --skip-web)
      DO_WEB=0
      shift
      ;;
    --no-smoke)
      DO_SMOKE=0
      shift
      ;;
    --no-sign)
      DO_SIGN=0
      shift
      ;;
    --dmg)
      DO_DMG=1
      shift
      ;;
    -h | --help)
      usage
      ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

# ─── 前置检查 ───────────────────────────────────────────────────────────────

preflight() {
  [[ "$(uname -s)" == "Darwin" ]] || die "只能在 macOS 上构建（当前 $(uname -s)）"
  [[ "$(uname -m)" == "arm64" ]] || die "只支持 Apple Silicon（当前 $(uname -m)）"
  local missing=()
  for cmd in uv pnpm python3 swiftc codesign; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  [[ ${#missing[@]} -eq 0 ]] || die "缺少构建工具：${missing[*]}"
}

resolve_version() {
  if [[ -n "$VERSION" ]]; then
    RESOLVED_PEP="$(python3 "$ROOT/scripts/version_normalize.py" "$VERSION" --target pep440)"
  else
    RESOLVED_PEP="$(
      uv run --no-project --with setuptools-scm python3 -c \
        "from setuptools_scm import get_version; print(get_version(version_scheme='no-guess-dev'))" \
        2>/dev/null || echo "0.0.0"
    )"
  fi
  [[ -n "$RESOLVED_PEP" ]] || die "版本解析失败"
  # CFBundleVersion 只允许数字与点：把 dev/local 段里的字母换成点。
  BUILD_NUMBER="$(printf '%s' "$RESOLVED_PEP" | sed 's/[^0-9.]/./g; s/\.\{2,\}/./g; s/^\.//; s/\.$//')"
  [[ -n "$BUILD_NUMBER" ]] || BUILD_NUMBER="1"
  log "版本：PEP440=$RESOLVED_PEP CFBundleVersion=$BUILD_NUMBER"
}

# ─── 1. 前端 ────────────────────────────────────────────────────────────────

build_web() {
  local static="$ROOT/backend/miloco/src/miloco/static"
  log "构建 web 前端 ..."
  rm -rf "$static/assets" "$static/index.html" "$static/fonts" \
    "$static/favicon.svg" "$static/watch.html" "$static/vendor"
  mkdir -p "$static"
  (
    cd "$ROOT/web"
    CI=true pnpm install --frozen-lockfile
    MILOCO_APP_VERSION="$RESOLVED_PEP" pnpm build
  )
  local item
  for item in index.html assets fonts favicon.svg watch.html vendor; do
    [[ -e "$ROOT/web/dist/$item" ]] && cp -R "$ROOT/web/dist/$item" "$static/"
  done
  rm -f "$static/assets/"*.map
  local required
  for required in index.html assets fonts watch.html; do
    [[ -e "$static/$required" ]] || die "web 构建产物缺失：$required"
  done
}

# ─── 2. wheel ───────────────────────────────────────────────────────────────

build_wheels() {
  local whl_dir="$OUT_DIR/build/wheels"
  rm -rf "$whl_dir"
  mkdir -p "$whl_dir"
  export SETUPTOOLS_SCM_PRETEND_VERSION="$RESOLVED_PEP"

  log "构建 miloco wheel ..."
  (cd "$ROOT/backend/miloco" && uv build --wheel --out-dir "$whl_dir" >/dev/null)

  log "构建 miloco-miot wheel（darwin/arm64）..."
  local stage="$OUT_DIR/build/miot"
  rm -rf "$stage"
  mkdir -p "$stage/src/miot/libs/darwin/arm64"
  rsync -a --exclude='libs/' "$ROOT/backend/miot/src/miot/" "$stage/src/miot/"
  cp "$ROOT/backend/miot/src/miot/libs/darwin/arm64/"* "$stage/src/miot/libs/darwin/arm64/"
  cp "$ROOT/backend/miot/pyproject.toml" "$stage/pyproject.toml"
  (cd "$stage" && uv build --wheel --out-dir "$whl_dir" >/dev/null)

  # uv build 出的是 py3-none-any，必须重打成 macosx_11_0_arm64（与 scripts/build.sh 同法）。
  local built
  built="$(ls "$whl_dir"/miloco_miot-*-py3-none-any.whl | head -1)"
  uv run --no-project --with wheel python3 -m wheel tags \
    --platform-tag "macosx_11_0_arm64" "$built" >/dev/null
  [[ -f "$built" && "$built" != *"macosx_11_0_arm64"* ]] && rm -f "$built"

  MILOCO_WHL="$(ls "$whl_dir"/miloco-*.whl | grep -v miloco_miot | head -1)"
  MIOT_WHL="$(ls "$whl_dir"/miloco_miot-*macosx_11_0_arm64.whl | head -1)"
  [[ -n "$MILOCO_WHL" && -n "$MIOT_WHL" ]] || die "wheel 构建失败"
  log "wheel：$(basename "$MILOCO_WHL") + $(basename "$MIOT_WHL")"
}

# ─── 3. 内置 Python ─────────────────────────────────────────────────────────

install_python() {
  local res="$APP/Contents/Resources"
  log "准备内置 CPython $PY_VERSION ..."
  uv python install "$PY_VERSION" >/dev/null
  local py_bin py_root
  py_bin="$(uv python find --no-project "$PY_VERSION")"
  py_root="$(cd "$(dirname "$py_bin")/.." && pwd -P)" # 解析 uv 的版本符号链接

  rm -rf "$res/py"
  # -L：uv 的 cpython-3.12-... 是指向 cpython-3.12.x-... 的符号链接，必须拷实体。
  cp -RL "$py_root" "$res/py"
  # 这个解释器归 App 所有，去掉 uv 的 externally-managed 标记才能直接往里装包。
  rm -f "$res/py/lib/python$PY_VERSION/EXTERNALLY-MANAGED"
  prune_python "$res/py"

  log "安装精简依赖 ..."
  local py="$res/py/bin/python3"
  uv pip install --python "$py" --no-deps "$MILOCO_WHL" "$MIOT_WHL" >/dev/null
  uv pip install --python "$py" "${SLIM_DEPS[@]}" >/dev/null

  local banned
  for banned in "${BANNED_PACKAGES[@]}"; do
    if "$py" -c "import $banned" >/dev/null 2>&1; then
      die "slim 版不应包含 ${banned}（依赖裁剪失效）"
    fi
  done
  # 预热字节码（运行期设了 PYTHONDONTWRITEBYTECODE，不会往签名包里写文件）。
  "$py" -m compileall -q "$res/py/lib/python$PY_VERSION/site-packages" >/dev/null || warn "compileall 部分失败（忽略）"
  # 兜底禁写字节码：任何人在签名后调用内嵌解释器补写 .pyc 都会让 ad-hoc 签名的封条失效
  # （构建期看不到，只有运行时 codesign --verify 才会报 sealed resource invalid）。
  # 放在 compileall 之后：否则连预热都写不出来。
  cp "$APP_DIR/runtime/sitecustomize.py" \
    "$res/py/lib/python$PY_VERSION/site-packages/sitecustomize.py"
  # 以 launcher 的真实环境（slim + 私有 MILOCO_HOME）做导入自检：必须能 import 主模块，
  # 且 person/pet/home_profile 路由（唯一使用 Form/File → 需要 python-multipart 的地方）
  # 不得注册。这一步同时守住了“重依赖裁剪失效”和“slim 路由门禁失效”两类问题。
  local check_home
  check_home="$(mktemp -d)"
  if ! MILOCO_HOME="$check_home" MILOCO_EDITION=slim PYTHONDONTWRITEBYTECODE=1 "$py" - <<'PYCHECK'
import sys

import miloco.main as main

import av  # noqa: F401
import cv2  # noqa: F401
import miot  # noqa: F401

paths = {getattr(route, "path", "") for route in main.app.routes}
for banned in ("/api/person", "/api/pet", "/api/home_profile"):
    offenders = sorted(p for p in paths if p.startswith(banned))
    if offenders:
        sys.exit(f"slim 不应注册 {banned} 路由：{offenders[:3]}")
if "/health" not in paths:
    sys.exit("/health 路由缺失")
print("SLIM_IMPORT_OK")
PYCHECK
  then
    rm -rf "$check_home"
    die "内置解释器自检失败（见上方输出）"
  fi
  rm -rf "$check_home"
}

# 删掉与运行无关的标准库与工具，57MB → ~40MB。
prune_python() {
  local py_dir="$1"
  local stdlib="$py_dir/lib/python$PY_VERSION"
  rm -rf \
    "$stdlib/test" "$stdlib/idlelib" "$stdlib/tkinter" "$stdlib/turtledemo" \
    "$stdlib/ensurepip" "$stdlib/lib2to3" "$stdlib/unittest/test" \
    "$py_dir/lib/tcl"* "$py_dir/lib/tk"* "$py_dir/lib/itcl"* \
    "$py_dir/lib/thread"* "$py_dir/lib/pkgconfig" \
    "$py_dir/include" "$py_dir/share"
  rm -f "$py_dir/bin/2to3"* "$py_dir/bin/idle3"* "$py_dir/bin/pydoc3"*
  return 0
}

# ─── 4. App 骨架 ────────────────────────────────────────────────────────────

assemble_skeleton() {
  local contents="$APP/Contents"
  log "组装 App 骨架 ..."
  rm -rf "$APP"
  mkdir -p "$contents/MacOS" "$contents/Resources/defaults"

  cp "$APP_DIR/defaults/config.json" "$contents/Resources/defaults/config.json"
  sed -e "s/@VERSION@/$RESOLVED_PEP/g" \
    -e "s/@BUILD_NUMBER@/$BUILD_NUMBER/g" \
    -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" \
    "$APP_DIR/launcher/Info.plist" >"$contents/Info.plist"

  log "编译 Swift launcher ..."
  swiftc -O -target "arm64-apple-macos$TARGET_MACOS" \
    -framework AppKit -o "$contents/MacOS/Miloco" "$APP_DIR/launcher/main.swift"

  # 菜单栏图标：直接用矢量 SVG（AppKit _NSSVGImageRep 渲染，透明背景 + 模板单色）。
  # 不能复用下面的 icns —— QuickLook 渲出来的 PNG 背景不透明，菜单栏上会是一块白边方块。
  if [[ -f "$APP_DIR/launcher/MenuBarIcon.svg" ]]; then
    cp "$APP_DIR/launcher/MenuBarIcon.svg" "$contents/Resources/MenuBarIcon.svg"
    log "已放入菜单栏图标 MenuBarIcon.svg"
  else
    warn "缺少 app/launcher/MenuBarIcon.svg，菜单栏将退回 App 图标"
  fi

  # 图标：把 web 的 favicon.svg 用 QuickLook 渲成 PNG 再打 icns；失败就留系统默认图标。
  if make_icon "$contents/Resources/Miloco.icns"; then
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string Miloco" "$contents/Info.plist" \
      >/dev/null 2>&1 || true
    log "已生成图标 Miloco.icns"
  else
    warn "未生成图标（favicon 渲染失败），使用系统默认图标"
  fi

  printf 'APPL????' >"$contents/PkgInfo"
}

make_icon() {
  local out="$1"
  local src="$ROOT/web/public/favicon.svg"
  [[ -f "$src" ]] || return 1
  local tmp
  tmp="$(mktemp -d)"
  qlmanage -t -s 1024 -o "$tmp" "$src" >/dev/null 2>&1 || {
    rm -rf "$tmp"
    return 1
  }
  local png
  png="$(ls "$tmp"/*.png 2>/dev/null | head -1)"
  [[ -n "$png" ]] || {
    rm -rf "$tmp"
    return 1
  }
  local iconset="$tmp/Miloco.iconset"
  mkdir -p "$iconset"
  local size
  for size in 16 32 64 128 256 512; do
    sips -z "$size" "$size" "$png" --out "$iconset/icon_${size}x${size}.png" >/dev/null 2>&1
    sips -z "$((size * 2))" "$((size * 2))" "$png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null 2>&1
  done
  iconutil -c icns "$iconset" -o "$out" >/dev/null 2>&1 || {
    rm -rf "$tmp"
    return 1
  }
  rm -rf "$tmp"
  return 0
}

# ─── 5. 签名 / 打包 ─────────────────────────────────────────────────────────

sign_app() {
  [[ "$DO_SIGN" == "1" ]] || return 0
  log "ad-hoc 签名（内嵌 Mach-O → 主程序 → bundle）..."
  local f
  # 先签所有内嵌动态库/扩展模块，最后签主可执行文件与 bundle，避免破坏 bundle 封印。
  while IFS= read -r -d '' f; do
    [[ "$f" == "$APP/Contents/MacOS/Miloco" ]] && continue
    if file -b "$f" 2>/dev/null | grep -q "Mach-O"; then
      codesign --force --sign - --timestamp=none "$f" >/dev/null 2>&1 || warn "签名失败：$f"
    fi
  done < <(find "$APP" -type f \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) -print0)
  codesign --force --sign - --timestamp=none "$APP/Contents/MacOS/Miloco" >/dev/null 2>&1 ||
    warn "主程序签名失败"
  codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 ||
    warn "bundle 签名失败（用户仍可用 xattr -dr com.apple.quarantine 打开）"
  codesign --verify --deep --strict "$APP" >/dev/null 2>&1 &&
    log "签名校验通过" || warn "签名校验未通过（ad-hoc 下可接受）"
}

make_dmg() {
  local dmg="$OUT_DIR/Miloco-$RESOLVED_PEP-arm64.dmg"
  log "打包 dmg ..."
  local stage
  stage="$(mktemp -d)"
  cp -R "$APP" "$stage/"
  ln -s /Applications "$stage/Applications"
  rm -f "$dmg"
  hdiutil create -volname "Miloco" -srcfolder "$stage" -ov -format UDZO "$dmg" >/dev/null
  rm -rf "$stage"
  log "dmg：${dmg}（$(du -h "$dmg" | cut -f1)）"
}

# ─── 主流程 ─────────────────────────────────────────────────────────────────

unregister_bundle() {
  # 让仓库里的构建产物不出现在启动台。注销只删 LaunchServices 的注册记录、不动文件；
  # 直接跑 Contents/MacOS/Miloco 或 app/smoke_test.sh <path> 都不受影响。
  local lsr
  lsr="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
  [[ -x "$lsr" ]] || return 0
  "$lsr" -u "$APP" >/dev/null 2>&1 || true
}

main() {
  preflight
  resolve_version
  mkdir -p "$OUT_DIR"
  # 构建产物放在仓库里，而启动台的图标列表来自 **LaunchServices 注册表**（外加 Spotlight
  # 索引）：只要 `open` 过 dist/app/Miloco.app，它就会被注册，之后装到 /Applications 的
  # 那份一起，启动台就出现两个同名 Miloco（实测：lsregister 里同时注册了两条）。两道保险：
  #   1. .metadata_never_index 让 Spotlight 跳过整棵 dist/ 树；
  #   2. 构建收尾 unregister_bundle 把本次产物从 LaunchServices 注销。
  touch "$ROOT/dist/.metadata_never_index"

  [[ "$DO_WEB" == "1" ]] && build_web || log "跳过前端构建"
  build_wheels
  assemble_skeleton
  install_python
  sign_app

  local size
  size="$(du -sh "$APP" | cut -f1)"
  log "App 大小：$size"
  log "App 路径：$APP"

  if [[ "$DO_SMOKE" == "1" ]]; then
    bash "$APP_DIR/smoke_test.sh" "$APP" || die "冒烟测试失败"
    # 冒烟测试真跑了一遍 App，必须复核签名：如果它（或任何后续步骤）往包里补写了
    # __pycache__ 之类文件，ad-hoc 签名的封条就废了 —— 这种问题只有在这里拦得住。
    if ! codesign --verify --deep --strict "$APP" >/dev/null 2>&1; then
      codesign --verify --verbose=2 --strict "$APP" >&2 || true
      die "冒烟测试后签名失效（App 包被改动过，用户首次打开会被 Gatekeeper 拦下）"
    fi
    log "冒烟测试后签名仍然有效"
  fi
  [[ "$DO_DMG" == "1" ]] && make_dmg
  unregister_bundle

  log "完成 ✅  （安装：拖到 /Applications，首次打开前执行"
  log "         xattr -dr com.apple.quarantine /Applications/Miloco.app ）"
}

main
