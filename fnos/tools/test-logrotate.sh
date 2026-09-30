#!/bin/bash
# 日志轮转行为测试 —— 从真实脚本里【提取】函数再跑，不复制代码（否则会漂移）。
#
# 两条最关键的断言：
#   1. 轮转后 inode 不变                        —— 守住「不能 mv/rename」
#   2. 轮转后 O_APPEND 写入仍落在同一文件        —— 真实模拟子进程持有的 fd
#
# 用法：./tools/test-logrotate.sh [cmd/main 路径]
set -u

MAIN="${1:-$(cd "$(dirname "$0")/.." && pwd)/cmd/main}"
[ -f "$MAIN" ] || { echo "找不到 $MAIN"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOG="$TMP/app.log"

# trim_log 里会调用 log_msg，这里提供同名实现
log_msg() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$TMP/boot.log"; }

# ★ 从真实脚本里提取函数
sed -n '/^trim_log() {/,/^}$/p' "$MAIN" > "$TMP/trim_log.sh"
if [ ! -s "$TMP/trim_log.sh" ]; then
    echo "❌ 没能从 $MAIN 提取到 trim_log()"
    exit 1
fi
# shellcheck disable=SC1090
source "$TMP/trim_log.sh"

fail=0
ok()  { echo "  ✅ $1"; }
bad() { echo "  ❌ $1"; fail=1; }

echo "=== 1) 限额内不动 ==="
printf 'small\n' > "$LOG"
trim_log "$LOG"
[ "$(stat -c %s "$LOG")" = "6" ] && ok "大小未变（6）" || bad "大小被改了：$(stat -c %s "$LOG")"

echo "=== 2) 超限后保留尾部、丢掉被截断的首行 ==="
LOG_MAX_BYTES=1000 LOG_KEEP_BYTES=200
export LOG_MAX_BYTES LOG_KEEP_BYTES
python3 - "$LOG" <<'PY'
import sys
with open(sys.argv[1], "wb") as f:
    for i in range(200):
        f.write(("line%04d\n" % i).encode())
PY
trim_log "$LOG"
SIZE=$(stat -c %s "$LOG")
FIRST=$(head -1 "$LOG")
echo "  轮转后大小 = $SIZE bytes，尾部首行 = [$FIRST]"
[ "$SIZE" -gt 0 ] && [ "$SIZE" -le 1000 ] && ok "已收缩到限额内" || bad "大小异常：$SIZE"
case "$FIRST" in
    line[0-9][0-9][0-9][0-9]) ok "首行是完整日志行（残缺行已被丢弃）" ;;
    *)                        bad "首行是残缺行：[$FIRST]" ;;
esac

echo "=== 3) 轮转后 inode 不变（不能 mv/rename）==="
INODE_BEFORE=$(stat -c %i "$LOG")
printf 'pad\n' >> "$LOG"
python3 -c "open('$LOG','ab').write(b'x'*3000)"
trim_log "$LOG"
INODE_AFTER=$(stat -c %i "$LOG")
SIZE3=$(stat -c %s "$LOG")
# 先确认这一步确实发生了轮转，否则 inode 断言是空转（永远为真）
[ "$SIZE3" -le "$LOG_MAX_BYTES" ] && ok "确实发生了轮转（大小 $SIZE3）" \
    || bad "这一段没触发轮转，inode 断言无意义：$SIZE3"
[ "$INODE_BEFORE" = "$INODE_AFTER" ] && ok "inode $INODE_AFTER 未变" \
    || bad "inode 变了 $INODE_BEFORE -> $INODE_AFTER"

echo "=== 4) 轮转后经同一 fd 写入仍落在该文件 ==="
# 必须在持有 fd 之前先把文件撑过限额，否则这一步同样不会触发轮转、断言空转
python3 -c "open('$LOG','ab').write(b'y'*3000)"
exec 3>>"$LOG"
trim_log "$LOG"
printf 'AFTER_ROTATE_MARKER\n' >&3
exec 3>&-
grep -q 'AFTER_ROTATE_MARKER' "$LOG" \
    && ok "标记仍存在（写入方 fd 未失效）" \
    || bad "内容丢失 —— 说明用了 mv/rename"

echo "=== 5) 幂等 ==="
S1=$(stat -c %s "$LOG"); trim_log "$LOG"; S2=$(stat -c %s "$LOG")
[ "$S1" = "$S2" ] && ok "已收缩的不再重复轮转（$S2）" || bad "非幂等：$S1 -> $S2"

echo "=== 6) 文件不存在时不报错 ==="
if trim_log "$TMP/does-not-exist.log" 2>/dev/null; then ok "静默返回 0"; else bad "返回非 0"; fi

echo "=== 7) 单行超过保留长度时不得把日志清空 ==="
python3 -c "open('$LOG','wb').write(b'z'*5000)"
trim_log "$LOG"
SIZE7=$(stat -c %s "$LOG")
[ "$SIZE7" -gt 0 ] && ok "单行日志仍保留 $SIZE7 字节" || bad "轮转把日志清空了"

echo "=== 8) 轮转时必须写进 boot.log（可观测）==="
grep -q 'logrotate' "$TMP/boot.log" && ok "已记录轮转事件" || bad "没有轮转记录"

echo
if [ "$fail" = "0" ]; then
    echo "全部日志轮转断言通过（10 条）"
else
    echo "存在失败断言"
fi
exit "$fail"
