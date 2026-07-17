#!/usr/bin/env bash
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
#
# miloco 全组件构建脚本（只构建到 dist/；发布统一走 .github/workflows/release.yml）
# 用法: scripts/build.sh [选项]
#
# 选项:
#   --version <ver>     覆盖所有包的版本号
#   --packages <list>   逗号分隔的包列表（miloco-miot,miloco,miloco-cli,openclaw,web）
#   -h, --help          显示帮助
#
# 注：每次构建前会默认清空 dist/
#
# 退出码: 0=成功, 1=构建失败, 4=前置检查失败

set -euo pipefail

# macOS 的 /usr/bin/tar 默认把扩展属性/资源叉打成 AppleDouble (._*) 影子文件塞进归档，
# 解压后会与真文件并存，污染 wheel/tgz/模型 glob。置此环境变量让 mac tar 不再带入；
# Linux 的 GNU tar 忽略该变量（无副作用），CI 与本地 mac 构建产物因此一致干净。
export COPYFILE_DISABLE=1

# ─── 常量 ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"
ALL_PACKAGES="miloco-miot,miloco,miloco-cli,openclaw,web"

# ─── 工具函数 ──────────────────────────────────────────────────────────────

log() { printf '[build] %s\n' "$*" >&2; }
die() { local code=$1; shift; log "ERROR: $*"; exit "$code"; }

sha256() {
    shasum -a 256 "$@" 2>/dev/null || sha256sum "$@"
}

# ─── 参数解析 ──────────────────────────────────────────────────────────────

VERSION=""
PACKAGES="$ALL_PACKAGES"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)   VERSION="$2"; shift 2 ;;
        --packages)  PACKAGES="$2"; shift 2 ;;
        -h|--help)
            sed -n '5,15p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0
            ;;
        *) die 4 "未知参数: $1" ;;
    esac
done

# ─── 辅助判断 ──────────────────────────────────────────────────────────────

should_build() {
    [[ ",$PACKAGES," == *",$1,"* ]]
}

# ─── 前置检查 ──────────────────────────────────────────────────────────────

check_prerequisites() {
    command -v uv >/dev/null 2>&1 || die 4 "uv 未安装"
    command -v python3 >/dev/null 2>&1 || die 4 "python3 未安装"

    if should_build "openclaw" || should_build "web"; then
        command -v pnpm >/dev/null 2>&1 || die 4 "pnpm 未安装"
        command -v npm >/dev/null 2>&1 || die 4 "npm 未安装"
    fi

    [ -f "$PROJECT_ROOT/backend/miot/pyproject.toml" ] || die 4 "不在项目根目录"
}

# ─── 版本号解析（不写源码）──────────────────────────────────────────────────
# Python 包版本由 hatch-vcs 从 git tag 派生（见各 pyproject [tool.hatch.version]），
# 这里把版本经 SETUPTOOLS_SCM_PRETEND_VERSION 注入：发布用 --version 的 CalVer，本地
# 缺省时用 setuptools_scm 从 git 派生的 dev 版。注入后所有 uv build（含 miot 在 git 外
# 的临时目录构建）拿到同一版本，且不改任何 pyproject / package.json，工作树保持干净。
# 结果写入全局：RESOLVED_RAW（= git tag，去 v）/ RESOLVED_PEP（PEP440）/ RESOLVED_NPM（semver）。
RESOLVED_RAW=""
RESOLVED_PEP=""
RESOLVED_NPM=""

resolve_version() {
    if [[ -n "$VERSION" ]]; then
        # 发布：VERSION 是 CalVer raw，规范化为 PEP440 / npm 两形态
        RESOLVED_RAW="$VERSION"
        RESOLVED_PEP=$(python3 "$SCRIPT_DIR/version_normalize.py" "$VERSION" --target pep440) || die 4 "版本号非法: $VERSION"
        RESOLVED_NPM=$(python3 "$SCRIPT_DIR/version_normalize.py" "$VERSION" --target npm)
    else
        # 本地：用 setuptools_scm 从 git 派生 PEP440 dev 版（与 uv sync 时 hatch-vcs 一致）。
        # version_scheme=no-guess-dev 必须与各 pyproject [tool.hatch.version].raw-options 一致：
        # CalVer 不猜"下一版"，以当前 tag 作 base + post 段（形如 2026.7.3.post1.devN+g<hash>）。
        RESOLVED_PEP=$(uv run --no-project --with setuptools-scm python3 -c \
            "from setuptools_scm import get_version; print(get_version(version_scheme='no-guess-dev'))" 2>/dev/null || echo "0.0.0")
        RESOLVED_RAW="$RESOLVED_PEP"
        # npm 包版本：把 PEP440 dev 版转成合法 semver（保留真实 base，如 2026.7.3-post1.dev117
        # +ge88bd38），不再用丢了 base 的 0.0.0 占位。web 不发布、openclaw 走本地 tgz 装，均
        # 不依赖 npm 版本排序，故 semver 里 dev 版 < 正式版无碍。转换失败兜底回 PEP 串（会让
        # npm version 报错以暴露异常，而非静默用假版本）。
        RESOLVED_NPM=$(python3 "$SCRIPT_DIR/version_normalize.py" "$RESOLVED_PEP" --pep2semver 2>/dev/null || echo "$RESOLVED_PEP")
    fi
    export SETUPTOOLS_SCM_PRETEND_VERSION="$RESOLVED_PEP"
    log "版本: pep440=$RESOLVED_PEP npm=$RESOLVED_NPM (raw=$RESOLVED_RAW)"
}

# 临时给 npm 包打版本号后还原 package.json，保证工作树不残留改动（CI 同样无害）。
restore_pkg_json() {
    local f="$1"
    if git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "$PROJECT_ROOT" checkout -- "$f" 2>/dev/null || true
    fi
}

# ─── 构建函数 ──────────────────────────────────────────────────────────────

fix_wheel_tag() {
    local whl="$1" platform_tag="$2"
    uv run --with wheel python3 -m wheel tags --platform-tag "$platform_tag" "$whl" >/dev/null
    local expected_name="${whl/py3-none-any/py3-none-$platform_tag}"
    if [[ -f "$whl" && "$whl" != "$expected_name" ]]; then
        rm -f "$whl"
    fi
}

build_miloco_miot() {
    log "构建 miloco-miot (多平台 wheel) ..."

    local miot_dir="$PROJECT_ROOT/backend/miot"
    local src_dir="$miot_dir/src/miot"
    local libs_dir="$src_dir/libs"
    local tmp_dir
    tmp_dir=$(mktemp -d)

    local platforms=(
        "darwin/arm64|macosx_11_0_arm64"
        "darwin/x86_64|macosx_10_9_x86_64"
        "linux/arm64|manylinux_2_28_aarch64" # glibc≥2.28
        "linux/x86_64|manylinux_2_28_x86_64" # glibc≥2.28
    )

    for entry in "${platforms[@]}"; do
        local src_sub="${entry%%|*}"
        local platform_tag="${entry##*|}"
        local lib_path="$libs_dir/$src_sub"

        if [[ ! -d "$lib_path" ]] || [[ -z "$(ls "$lib_path" 2>/dev/null)" ]]; then
            log "  WARN: libs/$src_sub 不存在或为空，跳过"
            continue
        fi

        log "  构建平台: $platform_tag ..."

        local build_dir="$tmp_dir/$platform_tag"
        mkdir -p "$build_dir/src/miot/libs/$src_sub"

        rsync -a --exclude='libs/' "$src_dir/" "$build_dir/src/miot/"
        cp "$lib_path"/* "$build_dir/src/miot/libs/$src_sub/"
        cp "$miot_dir/pyproject.toml" "$build_dir/pyproject.toml"

        (cd "$build_dir" && uv build --wheel --out-dir "$DIST_DIR")

        local built_whl
        built_whl=$(ls "$DIST_DIR"/miloco_miot-*-py3-none-any.whl 2>/dev/null | head -1)
        if [[ -n "$built_whl" ]]; then
            fix_wheel_tag "$built_whl" "$platform_tag"
            log "  -> miloco_miot-*-py3-none-${platform_tag}.whl"
        fi
    done

    rm -rf "$tmp_dir"
}

build_miloco() {
    log "构建 miloco ..."
    (cd "$PROJECT_ROOT/backend/miloco" && uv build --out-dir "$DIST_DIR")
}

build_miloco_cli() {
    log "构建 miloco-cli ..."
    (cd "$PROJECT_ROOT/cli" && uv build --out-dir "$DIST_DIR")
}

build_openclaw() {
    log "构建 openclaw 插件 ..."

    (
        cd "$PROJECT_ROOT/plugins/openclaw"
        # 临时写版本号供 npm pack 命名 tgz，pack 后由外层 restore_pkg_json 还原
        npm version "$RESOLVED_NPM" --no-git-tag-version --allow-same-version >/dev/null
        # 非交互场景（sync-to-remote 走 ssh）下，pnpm 检测到 packageManager 变更
        # 想删 node_modules 会因没 TTY 中止；CI=true 让 pnpm 自动跳过确认。
        CI=true pnpm install --frozen-lockfile
        pnpm build
        npm pack --pack-destination "$DIST_DIR"
    )
    restore_pkg_json plugins/openclaw/package.json
}

build_web() {
    log "构建 web 家庭面板 ..."
    local web_static="$PROJECT_ROOT/backend/miloco/src/miloco/static"
    # 清 static/ 下旧的 web 产物。vite emptyOutDir=false 不自清 dist,本地反复 build
    # 会累积旧 hashed chunk,一并清。static/ 整体不进 git(见 .gitignore),源码树初始
    # 状态下不存在 static/ 任何 web 资产,只有 build 后短暂出现用于打 wheel。
    rm -rf "$web_static/assets" "$web_static/index.html" "$web_static/fonts" \
        "$web_static/favicon.svg" "$web_static/watch.html" "$web_static/vendor"
    mkdir -p "$web_static"
    (
        cd "$PROJECT_ROOT/web"
        npm version "$RESOLVED_NPM" --no-git-tag-version --allow-same-version >/dev/null
        CI=true pnpm install --frozen-lockfile
        # vite.config.ts::outDir 是 "dist"(默认 web/dist),build 完后再 cp 到 backend
        # static_dir,让 backend wheel 打包时一并带上。改 vite outDir 直接指 backend
        # 会跟 dev 期 vite serve 的产物路径冲突,统一用 dist + cp 更清晰。
        # MILOCO_APP_VERSION 注入界面版本(vite define __APP_VERSION__)：用 RESOLVED_PEP,与
        # CLI/Python 包权威版本同源一致,界面不再显示上面 npm version 的装配占位 0.0.0-dev.<sha>。
        MILOCO_APP_VERSION="$RESOLVED_PEP" pnpm build
    )
    restore_pkg_json web/package.json
    # cp dist 内容到 backend static_dir,组装进 wheel。watch.html / vendor 的源仍
    # 在 web/public(唯一真源),vite build 已把 public/* 拷进 dist;这里和 index.html
    # /assets 同等处理,把「构建产物」落进 static,让 backend wheel 带上可直接 serve
    # 的真文件。static/ 不进 git,源码树初始状态下不出现这些文件。
    for item in index.html assets fonts favicon.svg watch.html vendor; do
        if [[ -e "$PROJECT_ROOT/web/dist/$item" ]]; then
            cp -R "$PROJECT_ROOT/web/dist/$item" "$web_static/"
        fi
    done
    # 清 sourcemap，防 LAN 直链 /assets/*.map 拿完整 TS 源
    rm -f "$web_static/assets/"*.map
    # 必需产物（vite build 出来的）：缺任一即视为构建坏，硬退出。否则 build_miloco
    # 会把残缺的 static 一并打进 wheel，发布后住户访问 1810 拿到死页才发现。
    # fonts 也列为必需——index.html `<link rel="preload" href="/fonts/...">` 强制
    # 引用，缺了 fonts 浏览器 console 会一直爆 404 + 字号退化到 monospace fallback。
    # watch.html 也是必需真文件(从 web/dist 落,缺了 /watch 页 503),这里硬校验防漏拷。
    if [[ ! -f "$web_static/index.html" || ! -d "$web_static/assets" \
          || ! -d "$web_static/fonts" || ! -f "$web_static/watch.html" ]]; then
        die 5 "web build 产物缺失（$web_static），wheel 会带上残缺 web"
    fi
}

# ─── manifest.json 更新 ───────────────────────────────────────────────────

update_manifest() {
    log "更新 manifest.json ..."

    # 版本号两种形态，分开维护（均由 resolve_version 解析好）：
    #   - pep → PEP440（$RESOLVED_PEP）：wheel 文件名用它；manifest.version 也存它（欢迎显示 + 安装缓存键）。
    #   - raw → 原始 git tag（$RESOLVED_RAW，如 2026.6.17 / 2026.6.17-beta.1）：下载 tag = v$raw、平台
    #     归档文件名用它，与 GitHub Release ref tag 对齐（PEP440 的 b1 形式与 git tag 的 -beta.1 不一致）。
    local raw="$RESOLVED_RAW" pep="$RESOLVED_PEP"

    local plugin_json="$PROJECT_ROOT/plugins/openclaw/openclaw.plugin.json"

    # scripts/manifest.json 是仓库内模板（仅维护 download.sites 镜像源），
    # 构建只读它、把填好 version/tag/tools/bundles 的成品写进 dist/manifest.json，
    # 不再回写仓库（生成物不进 VCS）。pack_install_scripts 内嵌的也是 dist/ 这份。
    python3 -c "
import json, pathlib

tpl_path = pathlib.Path('$SCRIPT_DIR/manifest.json')
out_path = pathlib.Path('$DIST_DIR/manifest.json')
plugin_path = pathlib.Path('$plugin_json')

tools = []
if plugin_path.is_file():
    plugin = json.loads(plugin_path.read_text())
    tools = plugin.get('contracts', {}).get('tools', [])

manifest = json.loads(tpl_path.read_text())
manifest['version'] = '$pep'   # 版本号：install.py 用于欢迎显示 + 安装缓存键（PEP440 形式）
manifest['tools'] = tools
# 下载源 tag 用 raw（= git tag）：自包含安装脚本据此从 .../releases/download/v{raw}/<bundle>
# 按版本下载平台归档，与 GitHub Release ref tag 对齐，确保可复现。
manifest.setdefault('download', {})['tag'] = 'v$raw'
manifest['bundles'] = {}   # 占位；pack_platform_bundles 算完各归档 SHA 后回填
# 清理旧 schema 残留（PyPI/npm 三源时代字段）
manifest.pop('npm_version', None)
manifest.pop('models', None)
manifest.get('download', {}).pop('tags', None)
manifest.get('download', {}).pop('dest', None)

out_path.write_text(json.dumps(manifest, indent=2) + '\n')
print(f'  {len(tools)} 个工具, 版本 {manifest[\"version\"]}, 下载 tag v$raw')
"
}

# ─── 模型打包 ────────────────────────────────────────────────────────────

pack_models() {
    local models_dir="$PROJECT_ROOT/backend/miloco/src/miloco/perception/models"
    if [ ! -d "$models_dir" ] || [ -z "$(ls -A "$models_dir"/*.onnx 2>/dev/null)" ]; then
        # 硬失败而非跳过：源码目录缺 .onnx 是异常状态（正常 clone 该有），
        # 空跳只会让下游 pack_platform_bundles 一起空跳、install.py 收到"缺模型" fail，
        # 排查链路变长。直接在源头中止。
        log "FATAL: $models_dir 缺 .onnx"
        exit 1
    fi

    local tar_name="miloco-models-${RESOLVED_PEP}.tar.gz"
    log "打包模型: $tar_name ..."
    tar -czf "$DIST_DIR/$tar_name" -C "$models_dir" .
    log "  $(du -h "$DIST_DIR/$tar_name" | cut -f1) $tar_name"
}

# ─── 平台归档打包 ─────────────────────────────────────────────────────────
# 按平台拆分的「代码 + 模型」一体归档（终端下载单文件即装齐）：每平台 = 该平台 miot
# wheel + miloco + cli wheel + openclaw tgz + 模型 tarball。打完算各归档 SHA/size 回填
# manifest.bundles（install.py 下载前据此选包 + 整包校验）。
pack_platform_bundles() {
    local models_tar miloco_whl cli_whl tgz
    models_tar=$(ls "$DIST_DIR"/miloco-models-*.tar.gz 2>/dev/null | head -1)
    miloco_whl=$(ls "$DIST_DIR"/miloco-*.whl 2>/dev/null | grep -v miloco_miot | grep -v miloco_cli | head -1)
    cli_whl=$(ls "$DIST_DIR"/miloco_cli-*.whl 2>/dev/null | head -1)
    tgz=$(ls "$DIST_DIR"/miloco-openclaw-plugin-*.tgz 2>/dev/null | head -1)

    # 子集构建（--packages 只构建部分包）本就产不出完整平台归档：跳过而非硬失败，
    # 否则 sync-to-remote.sh --packages <子集> 这类 dev 工作流会被 set -e 中断。
    # 全量构建（默认 / CI）时缺文件仍走硬失败，暴露 CI 打包异常。
    if [[ "$PACKAGES" != "$ALL_PACKAGES" ]]; then
        log "子集构建（--packages=$PACKAGES），跳过平台归档打包"
        return
    fi

    if [[ -z "$miloco_whl" || -z "$cli_whl" || -z "$tgz" || -z "$models_tar" ]]; then
        log "FATAL: 缺失文件 — miloco_whl=$miloco_whl cli_whl=$cli_whl tgz=$tgz models_tar=$models_tar"
        exit 1
    fi

    # raw（= git tag，去掉前导 v）：归档文件名用它，与下载 tag / GitHub Release ref 对齐。
    local raw="$RESOLVED_RAW"

    # 平台键（{os}-{arch}，与 install.py Platform.detect 对齐）| 该平台 miot wheel 平台 tag
    local platforms=(
        "darwin-arm64|macosx_11_0_arm64"
        "darwin-x86_64|macosx_10_9_x86_64"
        "linux-x86_64|manylinux_2_28_x86_64"
        "linux-aarch64|manylinux_2_28_aarch64"
    )

    log "打包平台归档（含模型）..."
    local tmp_root
    tmp_root=$(mktemp -d)

    for entry in "${platforms[@]}"; do
        local key="${entry%%|*}" ptag="${entry##*|}"
        local miot_whl
        miot_whl=$(ls "$DIST_DIR"/miloco_miot-*"$ptag"*.whl 2>/dev/null | head -1)
        if [[ -z "$miot_whl" ]]; then
            log "  WARN: 缺 $ptag 的 miot wheel，跳过 $key"
            continue
        fi
        local stage="$tmp_root/$key"
        mkdir -p "$stage"
        cp "$miloco_whl" "$cli_whl" "$miot_whl" "$tgz" "$models_tar" "$stage/"
        local bundle="miloco-$key-$raw.tar.gz"
        tar -czf "$DIST_DIR/$bundle" -C "$stage" .
        log "  $(du -h "$DIST_DIR/$bundle" | cut -f1) $bundle"
    done
    rm -rf "$tmp_root"

    # 算各归档 SHA/size 回填 dist/manifest.bundles（不碰仓库内模板）
    python3 -c "
import json, hashlib, pathlib
dist = pathlib.Path('$DIST_DIR')
manifest_path = dist / 'manifest.json'
manifest = json.loads(manifest_path.read_text())
bundles = {}
for key in ['darwin-arm64', 'darwin-x86_64', 'linux-x86_64', 'linux-aarch64']:
    f = dist / f'miloco-{key}-$raw.tar.gz'
    if not f.is_file():
        continue
    data = f.read_bytes()
    bundles[key] = {'name': f.name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
manifest['bundles'] = bundles
manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
print(f'  写入 {len(bundles)} 个平台归档到 manifest.bundles')
"
}

# ─── 自包含安装脚本 ───────────────────────────────────────────────────────

pack_install_scripts() {
    local scripts_dir="$PROJECT_ROOT/scripts"
    local out_dir="$DIST_DIR"   # 自包含脚本是构建产物，只进 dist/，不落仓库根

    local setup_py="$scripts_dir/install.py"
    local manifest="$DIST_DIR/manifest.json"   # 内嵌已填好 version/bundles 的成品 manifest
    local i18n_en="$scripts_dir/i18n/en.json"
    local i18n_zh="$scripts_dir/i18n/zh.json"
    local tpl_sh="$scripts_dir/install.sh"
    local tpl_ps1="$scripts_dir/install.ps1"

    for f in "$setup_py" "$manifest" "$i18n_en" "$i18n_zh" "$tpl_sh" "$tpl_ps1"; do
        [[ -f "$f" ]] || { log "跳过自包含脚本打包: $f 不存在"; return; }
    done

    local marker="# __SELF_CONTAINED__"

    log "打包自包含安装脚本..."

    # ── helper: encode resources as bash heredocs ─────────
    _embed_sh_resource() {
        local label="$1" src="$2" dest_expr="$3"
        printf 'base64 -d << '\''%s'\'' > %s\n' "$label" "$dest_expr"
        base64 < "$src"
        printf '%s\n\n' "$label"
    }

    # ── helper: encode resources as PowerShell base64 ─────
    _embed_ps1_resource() {
        local src="$1" dest_expr="$2"
        printf '    [System.IO.File]::WriteAllBytes(%s, [Convert]::FromBase64String(@"\n' "$dest_expr"
        base64 < "$src"
        printf '"@))\n\n'
    }

    # --- install.sh (self-contained) ---
    local out_sh="$out_dir/install.sh"
    {
        # Part before the marker — the bootstrap functions
        sed -n "1,/^${marker}/{ /^${marker}/!p; }" "$tpl_sh"

        # Injected self-contained preamble + resources
        cat << 'INJECT_SH'
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
mkdir -p "$WORK_DIR/i18n"

INJECT_SH
        _embed_sh_resource B64_SETUP_PY     "$setup_py"        '"$WORK_DIR/install.py"'
        _embed_sh_resource B64_MANIFEST     "$manifest"        '"$WORK_DIR/manifest.json"'
        _embed_sh_resource B64_I18N_EN      "$i18n_en"         '"$WORK_DIR/i18n/en.json"'
        _embed_sh_resource B64_I18N_ZH      "$i18n_zh"         '"$WORK_DIR/i18n/zh.json"'

        # Part after the marker — rewritten: SCRIPT_DIR→WORK_DIR, drop dirname line, exec→plain call
        sed -n "/^${marker}/,\${ /^${marker}/!p; }" "$tpl_sh" \
            | sed '/dirname.*BASH_SOURCE/d' \
            | sed 's/SCRIPT_DIR/WORK_DIR/g; s/^exec //'
    } > "$out_sh"
    chmod +x "$out_sh"

    # --- install.ps1 (self-contained) ---
    local out_ps1="$out_dir/install.ps1"
    {
        # Part before the marker
        sed -n "1,/^${marker}/{ /^${marker}/!p; }" "$tpl_ps1"

        # Injected self-contained preamble + resources
        cat << 'INJECT_PS1'
$script:WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) ("miloco-setup-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $script:WorkDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $script:WorkDir "i18n") -Force | Out-Null

INJECT_PS1
        _embed_ps1_resource "$setup_py"        '(Join-Path $script:WorkDir "install.py")'
        _embed_ps1_resource "$manifest"        '(Join-Path $script:WorkDir "manifest.json")'
        _embed_ps1_resource "$i18n_en"         '(Join-Path (Join-Path $script:WorkDir "i18n") "en.json")'
        _embed_ps1_resource "$i18n_zh"         '(Join-Path (Join-Path $script:WorkDir "i18n") "zh.json")'

        # Part after the marker — rewritten: ScriptDir→WorkDir, wrap in try/finally for cleanup
        sed -n "/^${marker}/,\${ /^${marker}/!p; }" "$tpl_ps1" \
            | sed 's/\$ScriptDir/$script:WorkDir/g; s/ScriptDir/script:WorkDir/g' \
            | sed '/^\$script:WorkDir = Split-Path/d' \
            | sed '/^exit \$LASTEXITCODE/d' \
            | awk '/& \$script:UvCmd run/ {
                print "try {"
                print "    " $0
                print "    exit $LASTEXITCODE"
                print "} finally {"
                print "    if (Test-Path $script:WorkDir) {"
                print "        Remove-Item -Recurse -Force $script:WorkDir -ErrorAction SilentlyContinue"
                print "    }"
                print "}"
                next
            } { print }'
    } > "$out_ps1"

    local sh_size ps1_size
    sh_size=$(du -h "$out_sh" | cut -f1)
    ps1_size=$(du -h "$out_ps1" | cut -f1)
    log "  $sh_size install.sh"
    log "  $ps1_size install.ps1"
}

# ─── 主流程 ────────────────────────────────────────────────────────────────

main() {
    cd "$PROJECT_ROOT"

    check_prerequisites

    # 清空 dist/，保证每次构建都是干净产物。
    log "清除 dist/ ..."
    rm -rf "$DIST_DIR"
    mkdir -p "$DIST_DIR"

    # 解析版本号（注入 SETUPTOOLS_SCM_PRETEND_VERSION，供后续所有构建拿到同一版本）
    resolve_version

    # 按依赖顺序构建
    # web 必须在 miloco 之前——build_web 把 dist 写进 backend/miloco/src/miloco/static，
    # build_miloco 打 wheel 时把 static 目录一起打包进 wheel
    if should_build "web";         then build_web; fi
    if should_build "miloco-miot"; then build_miloco_miot; fi
    if should_build "miloco";      then build_miloco; fi
    if should_build "miloco-cli";  then build_miloco_cli; fi
    if should_build "openclaw";    then build_openclaw; fi

    # 更新 manifest
    update_manifest

    # 打包模型
    pack_models

    # 按平台打「代码 + 模型」一体归档，并回填 manifest.bundles（须在自包含脚本前，
    # 让 pack_install_scripts 嵌入含 bundles 的完整 manifest）
    pack_platform_bundles

    # 自包含安装脚本
    pack_install_scripts

    # 列出产物
    log ""
    log "构建产物:"
    ls -lh "$DIST_DIR"/ 2>/dev/null | tail -n +2 | while read -r line; do
        log "  $line"
    done

    log ""
    log "完成!"
}

main "$@"
