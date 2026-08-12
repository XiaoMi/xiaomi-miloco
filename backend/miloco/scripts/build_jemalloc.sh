#!/usr/bin/env bash
# 在 manylinux2014 容器(glibc 2.17)里编 jemalloc, 产出可随项目分发的 libjemalloc.so.2。
#
# 为什么产物直接进 git, 而感知模型走 Release 附件: 两份 .so 合计约 1.4MB, 比模型小两个量级,
# 且只跟着 jemalloc 大版本走、几年才动一次 —— 换成下载会给安装链路多加一条网络失败路径,
# 省下的仓库体积不值这个代价。哪天它涨到几十 MB 或开始频繁更新, 再改走 Release 不迟。
# 用法: ./build_jemalloc.sh x86_64
#       ./build_jemalloc.sh aarch64        # 需先注册 binfmt, 见下方报错提示
#       OUT_DIR=/tmp/x ./build_jemalloc.sh x86_64
set -euo pipefail

TARGET_ARCH="${1:-$(uname -m)}"
# 钉 5.3.1: 换版本前先确认 configure.ac 的 LG_PAGE 分支和 src/pages.c 的 pages_boot 没变语义
# —— 下面 LG_PAGE 的取值理由建立在那两处上。
JEMALLOC_VERSION="${JEMALLOC_VERSION:-5.3.1}"
# 源码 tarball 的 sha256, 必须钉死: 产物要编进随包分发的二进制、塞到每台用户机器上,
# HTTPS 只保证传输安全,不保证"拿到的就是审过的那份代码"(release 资产可被替换、tag 可被移动)。
# 换版本时要同步换这个值,官方没有 GPG 签名,可用
#   curl -sfL https://api.github.com/repos/jemalloc/jemalloc/releases/tags/<ver> | grep digest
# 取 GitHub 侧记录的 digest,再与本地 sha256sum 对账。
declare -A JEMALLOC_SHA256=(
  [5.3.1]=3826bc80232f22ed5c4662f3034f799ca316e819103bdc7bb99018a421706f92
)
# 默认存储驱动用不了时的口子(例: overlay 跑在 ext4 上, podman 直接拒绝启动):
#   CONTAINER_FLAGS="--root /tmp/podman-root --runroot /tmp/podman-run --storage-driver vfs"
read -r -a CONTAINER_FLAG_ARR <<< "${CONTAINER_FLAGS:-}"
# 不加 --with-malloc-conf: 旋钮只有一个来源,就是 supervisord.conf 的 MALLOC_CONF。编进去会
# 多出一个改不动的隐藏默认值 —— 调 decay 时改了环境变量却不生效,而两处都"看起来"配过了。

# 镜像仓库前缀。quay.io 拉不动时换源用(实测国内经常 TLS handshake timeout):
#   IMAGE_PREFIX=ghcr.io/pypa ./build_jemalloc.sh x86_64
# 换源换的只是"从哪个 registry 取",取到的必须是同一份镜像 —— 下面按 digest 钉死,对不上
# 容器运行时会直接拒绝拉取。
#
# 为什么连镜像也要钉: 产物的字节由容器里的 gcc/binutils 决定,而 manylinux2014 是 pypa 持续
# 重建的滚动标签。不钉的话几个月后重跑,sha256 会因为工具链换代而静默漂移 —— 那时就无法区分
# "工具链变了"和"仓库里这份 .so 被人换过",而后者正是钉源码 sha256 想防的事。
IMAGE_PREFIX="${IMAGE_PREFIX:-quay.io/pypa}"
declare -A IMAGE_DIGEST=(
  [x86_64]=sha256:0a42cb7e5f4ba6bbfb8d0a86d1aab0c8876ba9c3be16bd99360ae42bf010ec77
  [aarch64]=sha256:63bfa74be47f0277e998cb7c1b571b27664ac848bb356b0f4588438f930285dd
)
# 仓库里当前那两份产物的 sha256。用上面钉死的镜像重跑应逐字节一致;不一致说明镜像或脚本变了,
# 查清楚再提交新的二进制,别直接覆盖。换 digest 时同步更新这里。
declare -A EXPECTED_SO_SHA256=(
  [x86_64]=39fe427335194e83638fb85a4b737c628548cd5ca8edf90fb2e811c809f690b6
  [aarch64]=02c1b02bbbd1bd4c0c052b747bb472aaf3c992faf6cb7f79111db6cdf29ec7f6
)

case "$TARGET_ARCH" in
  x86_64|amd64)
    TARGET_ARCH=x86_64
    IMAGE="$IMAGE_PREFIX/manylinux2014_x86_64@${IMAGE_DIGEST[x86_64]}"
    # x86_64 页大小固定 4K
    LG_PAGE=12
    LIB_SUBDIR=x86_64
    ;;
  aarch64|arm64)
    TARGET_ARCH=aarch64
    IMAGE="$IMAGE_PREFIX/manylinux2014_aarch64@${IMAGE_DIGEST[aarch64]}"
    # aarch64 有 4K/16K/64K 三种页, LG_PAGE 是编译期常量且必须 >= 运行时页大小。
    # 按最大页(64K)编, 才能同时跑在 4K/16K/64K 页内核上。
    LG_PAGE=16
    LIB_SUBDIR=arm64
    ;;
  *)
    echo "不支持的目标架构: $TARGET_ARCH (只支持 x86_64 / aarch64)" >&2
    exit 1
    ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
DEFAULT_OUT="$REPO_ROOT/backend/miot/src/miot/libs/linux/$LIB_SUBDIR"
OUT_DIR="${OUT_DIR:-$DEFAULT_OUT}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

container() { "$RUNTIME" ${CONTAINER_FLAG_ARR[@]+"${CONTAINER_FLAG_ARR[@]}"} "$@"; }

find_container_runtime() {
  for runtime in podman docker; do
    if command -v "$runtime" >/dev/null 2>&1 &&
      "$runtime" ${CONTAINER_FLAG_ARR[@]+"${CONTAINER_FLAG_ARR[@]}"} info >/dev/null 2>&1; then
      echo "$runtime"
      return 0
    fi
  done
  return 1
}

RUNTIME="$(find_container_runtime)" || {
  cat >&2 <<'MSG'
没有可用的容器运行时。装 podman 或启动 docker daemon 后重试。
若 podman 已装但报存储驱动错(overlay 跑在 ext4 上), 用独立存储目录绕过:
  CONTAINER_FLAGS="--root /tmp/podman-root --runroot /tmp/podman-run --storage-driver vfs" ./build_jemalloc.sh
MSG
  exit 1
}

can_run_foreign_arch() {
  [ "$TARGET_ARCH" = "$(uname -m)" ] && return 0
  # binfmt 按"要模拟哪种指令集"注册,名字跟着目标架构走 —— 在 arm 机上编 x86_64 要的是
  # qemu-x86_64,写死 qemu-aarch64 会把能跑的情况误判成跑不了。
  grep -qs enabled "/proc/sys/fs/binfmt_misc/qemu-$TARGET_ARCH" 2>/dev/null
}

if ! can_run_foreign_arch; then
  cat >&2 <<MSG
本机是 $(uname -m), 要编 $TARGET_ARCH 但没注册 binfmt, 容器跑不起来。三条路选一条:
  1. 注册 binfmt(需要 root):
       sudo $RUNTIME run --privileged docker.io/multiarch/qemu-user-static --reset -p yes
     Arch Linux 也可: sudo pacman -S qemu-user-static-binfmt && sudo systemctl restart systemd-binfmt
  2. 在目标机上直接跑这个脚本(目标机有容器运行时即可, 不需要交叉)。
  3. CI 上用原生 arm runner(GitHub Actions: ubuntu-24.04-arm)跑这个脚本。
MSG
  exit 1
fi

TARBALL="jemalloc-$JEMALLOC_VERSION.tar.bz2"
echo "== 下载 jemalloc $JEMALLOC_VERSION"
curl -fsSL --retry 3 -o "$WORK_DIR/$TARBALL" \
  "https://github.com/jemalloc/jemalloc/releases/download/$JEMALLOC_VERSION/$TARBALL"

EXPECTED_SHA="${JEMALLOC_SHA256[$JEMALLOC_VERSION]:-}"
if [[ -z "$EXPECTED_SHA" ]]; then
  echo "没有 jemalloc $JEMALLOC_VERSION 的 sha256 记录,拒绝构建。" >&2
  echo "先核对官方 digest 并把它加进脚本顶部的 JEMALLOC_SHA256。" >&2
  exit 1
fi
ACTUAL_SHA="$(sha256sum "$WORK_DIR/$TARBALL" | cut -d' ' -f1)"
if [[ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]]; then
  echo "源码 sha256 不符,中止构建。" >&2
  echo "  期望: $EXPECTED_SHA" >&2
  echo "  实际: $ACTUAL_SHA" >&2
  exit 1
fi
echo "   sha256 校验通过"

cat > "$WORK_DIR/build.sh" <<BUILD
#!/bin/bash
set -euo pipefail
tar xf "/io/$TARBALL" -C /tmp --no-same-owner
cd /tmp/jemalloc-$JEMALLOC_VERSION
./configure \
  --with-jemalloc-prefix= \
  --with-lg-page=$LG_PAGE \
  --with-lg-hugepage=21 \
  --disable-cxx \
  --disable-doc \
  --disable-initial-exec-tls > /tmp/configure.log 2>&1 || { tail -30 /tmp/configure.log; exit 1; }
make -j"\$(nproc)" > /tmp/make.log 2>&1 || { tail -30 /tmp/make.log; exit 1; }
cp lib/libjemalloc.so.2 /io/libjemalloc.so.2
strip --strip-unneeded /io/libjemalloc.so.2
echo "-- 依赖:"
ldd /io/libjemalloc.so.2
echo "-- 需要的最高 glibc 符号版本:"
objdump -T /io/libjemalloc.so.2 | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -uV | tail -1

# 校验 --with-lg-page 真的生效。参数名拼错或上游改了选项名时 configure 一般只警告不报错,
# 产物带着错的页大小交付,而架构/依赖校验抓不到 —— 后果是自带那份在目标机上初始化失败,
# 它又是最后一个候选,等于兜底静默失效。
#
# 读产物行为而非中间文件:mallctl("arenas.page") 返回的就是编译期 PAGE 常量。判据与运行时探针
# 同构(都是预加载后取无前缀 mallctl 符号)。"编译值 >= 运行时页大小就能跑"这条性质保证了在 4K
# 页的构建容器里也能读出 65536,不需要真有一台 64K 页机器。
echo "-- 校验 LG_PAGE 生效:"
cat > /tmp/check_page.c <<'CHECK_PAGE_C'
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stddef.h>
#include <stdio.h>

int main(void) {
    int (*mallctl)(const char *, void *, size_t *, void *, size_t) =
        dlsym(RTLD_DEFAULT, "mallctl");
    if (!mallctl) {
        fprintf(stderr, "jemalloc 没有接管: 取不到无前缀的 mallctl 符号\n");
        return 1;
    }
    size_t page = 0;
    size_t sz = sizeof(page);
    if (mallctl("arenas.page", &page, &sz, NULL, 0) != 0) {
        fprintf(stderr, "读 arenas.page 失败\n");
        return 1;
    }
    printf("%zu\n", page);
    return 0;
}
CHECK_PAGE_C
gcc -o /tmp/check_page /tmp/check_page.c -ldl
ACTUAL_PAGE="\$(LD_PRELOAD=/io/libjemalloc.so.2 /tmp/check_page)"
EXPECTED_PAGE=\$((1 << $LG_PAGE))
if [ "\$ACTUAL_PAGE" != "\$EXPECTED_PAGE" ]; then
  echo "LG_PAGE 没生效: arenas.page=\$ACTUAL_PAGE, 期望 \$EXPECTED_PAGE (--with-lg-page=$LG_PAGE)" >&2
  exit 1
fi
echo "   arenas.page = \$ACTUAL_PAGE = 1<<$LG_PAGE, 符合预期"
BUILD
chmod +x "$WORK_DIR/build.sh"

echo "== 在 $IMAGE 里编译 (LG_PAGE=$LG_PAGE)"
# 必须走 container() 而不是裸 "$RUNTIME": CONTAINER_FLAGS 里的 --root/--storage-driver 要跟着
# 每一条子命令,否则 run 会回落到默认 overlay 存储,在 ext4 上直接失败 —— 而 find_container_runtime
# 探测时带了 flags、报告"可用",于是失败发生在几分钟后的 run 而不是探测阶段。
# --network=none: 容器内不需要网络(源码 tarball 已在宿主下载并校验过 sha256、经 volume 挂进去,
# 编译只用本地文件)。除了杜绝"构建时偷偷取东西"这条供应链风险,还绕开了 rootless 网络的依赖 ——
# podman 的 pasta/slirp4netns 都要 /dev/net/tun,内核没这个设备时容器根本起不来。
# 用 --platform 而不是 podman 专有的 --arch: docker 不认 --arch,而 find_container_runtime
# 在没有 podman 时会选 docker。两者都认 --platform。
container run --rm --network=none \
  --platform "linux/$([ "$TARGET_ARCH" = x86_64 ] && echo amd64 || echo arm64)" \
  -v "$WORK_DIR:/io:z" -w /io "$IMAGE" /io/build.sh

BUILT_SO="$WORK_DIR/libjemalloc.so.2"
[ -s "$BUILT_SO" ] || { echo "编译结束但没拿到 .so, 中止。" >&2; exit 1; }

# 交付前校验: 架构对不对、有没有意外的第三方依赖
FILE_INFO="$(file -b "$BUILT_SO")"
echo "$FILE_INFO" | grep -q "$([ "$TARGET_ARCH" = x86_64 ] && echo x86-64 || echo aarch64)" || {
  echo "产物架构不符: $FILE_INFO" >&2
  exit 1
}
# 先把依赖列表取到变量再判内容: 管道放在 if 条件位上会让 set -e 失效, objdump 缺失(rc=127)
# 时条件为假、直接跳过检查 —— 一道防供应链问题的闸静默放行, 而旁边的架构检查会明确报错退出。
NEEDED="$(objdump -p "$BUILT_SO" | grep NEEDED)" || {
  echo "读不到产物的动态依赖(objdump 缺失或输出格式变了), 无法校验, 中止。" >&2
  exit 1
}
if grep -qvE "libc\.so|libm\.so|libdl\.so|libpthread\.so|ld-linux" <<<"$NEEDED"; then
  echo "产物有预期外的动态依赖:" >&2
  echo "$NEEDED" >&2
  exit 1
fi

# 与仓库记录比对: 同一个镜像 digest + 同一份源码,产物应该逐字节一致。不一致就是构建环境
# 变了(或有人动过仓库里那份),停下来让人看一眼,别默默覆盖。
BUILT_SHA="$(sha256sum "$BUILT_SO" | cut -d' ' -f1)"
EXPECTED_SO="${EXPECTED_SO_SHA256[$TARGET_ARCH]:-}"
if [[ -n "$EXPECTED_SO" && "$BUILT_SHA" != "$EXPECTED_SO" ]]; then
  echo "产物 sha256 与仓库记录不一致,构建环境可能已变:" >&2
  echo "  记录: $EXPECTED_SO" >&2
  echo "  本次: $BUILT_SHA" >&2
  echo "确认无误后同步更新脚本顶部的 EXPECTED_SO_SHA256 再提交。" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
cp "$BUILT_SO" "$OUT_DIR/libjemalloc.so.2"
echo "== 完成: $OUT_DIR/libjemalloc.so.2"
ls -l "$OUT_DIR/libjemalloc.so.2"
echo
echo "在目标机上用:"
echo "  LD_PRELOAD=$OUT_DIR/libjemalloc.so.2 <你的命令>"
echo "没有内置 MALLOC_CONF: 旋钮全靠运行时环境变量给(miloco 由 supervisord.conf 注入)"
