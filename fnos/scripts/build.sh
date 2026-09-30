#!/bin/bash
# AirConnect 飞牛应用 —— 可复现构建脚本
#
#   ./fnos/scripts/build.sh                # 从源码编译 airupnp/aircast 并打包 .fpk
#   ./fnos/scripts/build.sh --skip-native  # 复用 app/bin 里已有的二进制，只打包
#   ./fnos/scripts/build.sh --static       # 编译静态链接版本（自包含，体积大 8 倍）
#   ./fnos/scripts/build.sh --no-pack      # 只编译，不打 .fpk（CI 用）
#
# 产物：fnos/dist/AirConnect-<version>.fpk
#
# 说明：上游 AirConnect 把 common/* 子模块里各平台的静态库（libraop.a、libpupnp.a…）
# 直接提交进了仓库，所以本地/CI 编译只需要一个 x86_64 的 gcc，不需要交叉工具链，
# 也不需要先构建 OpenSSL 等依赖。
#
# 默认编译**动态链接**版本：实测产物只依赖 libc/libm（OpenSSL 是 dlopen 加载），
# 单文件约 0.9MB，且能跟随系统 OpenSSL 获得安全更新。若目标系统 glibc 过旧
# （需要 GLIBC_2.34，即 Debian 12 / fnOS 1.1.8 及以上），改用 --static。

set -euo pipefail

FNOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="$(cd "$FNOX_DIR/.." && pwd)"
DIST="$FNOX_DIR/dist"
# 规则 #10：**绝不在仓库目录里直接 fnpack build**（fnpack 会把被处理目录封成
# 0000 ACL，连属主都删不掉，且 cp -r 会把这个 ACL 复制出去）。一律用独立暂存目录。
# 暂存目录必须在仓库之外，且不能写死飞牛的路径 —— CI（GitHub Runner）根本没有
# /vol1，硬编码会直接 "mkdir: cannot create directory '/vol1': Permission denied"。
if [ -n "${AIRCONNECT_STAGE:-}" ]; then
    STAGE="$AIRCONNECT_STAGE"
elif [ -d /vol1/@apphome ]; then
    STAGE="/vol1/@apphome/airconnect/data/fpk-build"   # 飞牛 NAS 上放数据卷
else
    STAGE="${TMPDIR:-/tmp}/airconnect-fpk-build"       # CI / 其它机器
fi

SKIP_NATIVE=0
NO_PACK=0
LINK_MODE=dynamic
for arg in "$@"; do
    case "$arg" in
        --skip-native) SKIP_NATIVE=1 ;;
        --no-pack)     NO_PACK=1 ;;
        --static)      LINK_MODE=static ;;
        -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "未知参数：$arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "$FNOX_DIR/manifest" | tr -d '[:space:]')"
[ -n "$VERSION" ] || die "无法从 fnos/manifest 读取 version"
# fnpack 只接受 x.y.z 或 x.y.z-r，且每段必须是整数
case "$VERSION" in
    *[!0-9.-]*) die "manifest version 含非法字符：$VERSION（只允许数字、点、连字符）" ;;
esac
echo "$VERSION" | grep -Eq '^[0-9]+(\.[0-9]+){2}(-[0-9]+)?$' \
    || die "manifest version 必须形如 x.y.z 或 x.y.z-r（每段为整数），当前：$VERSION"

# ------------------------------------------------------------------ 原生编译
CC="${CC:-x86_64-linux-gnu-gcc}"
HOST=linux
PLATFORM=x86_64

ensure_submodules() {
    local missing=0
    for sub in common/libraop common/libmdns common/libpupnp common/libcodecs \
               common/libopenssl common/crosstools aircast/nanopb aircast/libjansson; do
        if [ ! -e "$ROOT_DIR/$sub/Makefile" ] && [ ! -e "$ROOT_DIR/$sub/build.sh" ] \
           && [ -z "$(ls -A "$ROOT_DIR/$sub" 2>/dev/null)" ]; then
            missing=1
        fi
    done
    if [ "$missing" = "1" ]; then
        log "初始化 AirConnect 子模块（首次构建需要，约 1–3 分钟）"
        ( cd "$ROOT_DIR" && git submodule update --init --depth 1 ) \
            || die "git submodule update 失败（需要网络访问 github.com）"
    fi
}

build_native() {
    command -v "$CC" >/dev/null 2>&1 || die "找不到编译器 $CC（apt-get install build-essential）"
    # 全新 clone 里 app/bin 不存在（被 .gitignore 排除），必须先建出来，
    # 否则 `install` 会因为父目录缺失而报一句与真正原因无关的错。
    mkdir -p "$FNOX_DIR/app/bin"
    ensure_submodules
    local target
    [ "$LINK_MODE" = "static" ] && target="-static" || target=""

    for item in airupnp aircast; do
        log "编译 $item（$LINK_MODE，HOST=$HOST PLATFORM=$PLATFORM）"
        ( cd "$ROOT_DIR/$item" && make CC="$CC" HOST="$HOST" PLATFORM="$PLATFORM" -j"$(nproc)" ) \
            || die "$item 编译失败"
        local produced="$ROOT_DIR/bin/$item-$HOST-$PLATFORM$target"
        [ -f "$produced" ] || die "编译产物缺失：$produced"
        install -m 755 "$produced" "$FNOX_DIR/app/bin/$item"
        # 二次 strip 兜底（上游 LDFLAGS 已带 -s）
        strip "$FNOX_DIR/app/bin/$item" 2>/dev/null || true
    done
}

verify_binaries() {
    log "校验桥接二进制"
    local bin
    for bin in airupnp aircast; do
        local path="$FNOX_DIR/app/bin/$bin"
        [ -x "$path" ] || die "缺少可执行文件：$path"
        # ⚠️ 不能用 `-h` 的退出码判断：AirConnect 的 -h 打印用法后 **exit(1)**，
        # 用退出码会把正常的二进制判成坏的。以输出内容为准。
        local first
        # 注意 `|| true`：`-h` 自身 exit 1，配合 set -o pipefail 会把脚本直接带崩
        first="$("$path" -h 2>&1 | head -1 || true)"
        echo "  $bin → $first"
        case "$first" in
            v1.12.*) ;;
            *) die "$bin 无法运行或版本异常（期望 v1.12.x）：$first" ;;
        esac
        if [ "$LINK_MODE" = "dynamic" ]; then
            if ldd "$path" 2>/dev/null | grep -qE 'not found'; then
                ldd "$path" >&2 || true
                die "$bin 存在未解析的动态库依赖"
            fi
        else
            if ldd "$path" 2>/dev/null | grep -qv 'not a dynamic executable'; then
                warn "$bin 不是完全静态链接"
            fi
        fi
    done
}

# ------------------------------------------------------------------ 打包
pack() {
    log "在独立暂存目录打包：$STAGE"
    # 源目录可能带 TrimACL：先给自己写权限，复制后立刻再 chmod 一次
    chmod -R u+rwX "$STAGE" 2>/dev/null || true
    rm -rf "$STAGE"
    mkdir -p "$STAGE"
    cp -r "$FNOX_DIR/." "$STAGE/"
    chmod -R u+rwX "$STAGE"

    # 清理不该进包的东西
    rm -rf "$STAGE/dist" "$STAGE/tests" "$STAGE/tools" "$STAGE/scripts" "$STAGE/README.md"
    find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true
    find "$STAGE" -name '.DS_Store' -delete 2>/dev/null || true

    # 权限归一化：先把所有普通文件压成 644，再单独给需要执行位的加回来。
    # （仓库里的文件权限受 umask 影响，不归一化会出现 manifest 变成 600 这种
    #   难以复现的差异。）
    find "$STAGE" -type f -exec chmod 644 {} + 2>/dev/null || true
    chmod 755 "$STAGE"/cmd/* || die "chmod cmd/* 失败"
    chmod 755 "$STAGE"/app/bin/* 2>/dev/null || true
    chmod 755 "$STAGE"/app/server/*.py 2>/dev/null || true
    chmod -R a+rX "$STAGE/app" "$STAGE/cmd" "$STAGE/config" "$STAGE/wizard" 2>/dev/null || true

    local fnpack="$FNOX_DIR/fnpack/fnpack-linux-amd64"
    [ -x "$fnpack" ] || die "缺少随仓库提交的 fnpack：$fnpack"

    mkdir -p "$DIST"
    local pack_log="$STAGE/.fnpack.log"
    ( cd "$STAGE" && "$fnpack" build ) > "$pack_log" 2>&1 || true
    # fnpack **打包失败时仍然返回退出码 0**，只信退出码会得到「静默无产物」，
    # 随后 mv 报一个与真正原因完全无关的错误。必须解析输出。
    if ! grep -q "Packing successfully" "$pack_log"; then
        echo "--- fnpack 输出 ---" >&2
        cat "$pack_log" >&2
        die "fnpack 打包失败（常见原因：manifest version 不合法、cmd 脚本缺执行位）"
    fi
    local out="$DIST/AirConnect-$VERSION.fpk"
    [ -f "$STAGE/airconnect.fpk" ] || die "fnpack 未产出 airconnect.fpk"
    mv -f "$STAGE/airconnect.fpk" "$out"
    log "打包完成：$out（$(du -h "$out" | cut -f1)）"
    echo "$out"
}

# ------------------------------------------------------------------ 主流程
log "AirConnect 飞牛应用构建（version=$VERSION，链接方式=$LINK_MODE）"
if [ "$SKIP_NATIVE" -eq 0 ]; then
    build_native
else
    log "跳过原生编译（--skip-native）"
fi
verify_binaries
if [ "$NO_PACK" -eq 1 ]; then
    log "已跳过 fnpack 打包（--no-pack）"
else
    pack
fi
