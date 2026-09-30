#!/bin/bash
# 升级路径测试 —— 专门针对两类「升级了但没人发现」的隐形故障：
#
#   1. upgrade_init 没有停服 → 旧进程占着端口，新版本 bind 失败，
#      表现在用户侧就是「升级了但功能没变」。
#   2. upgrade_callback 没有重新拉起服务 → 升级后应用直接是「已停止」。
#
# 做法：装 1.12.4-1 → 启动 → 用一个"新版本"的 fpk 走一遍 upgrade_init /
# upgrade_callback → 断言端口确实易主（PID 变了）、服务重新可用、且
# /api/health 报的是新版本号。
#
# 用法：./tools/test-upgrade.sh <old.fpk> <new.fpk>
set -u

OLD_FPK="${1:?用法: test-upgrade.sh <old.fpk> <new.fpk>}"
NEW_FPK="${2:?用法: test-upgrade.sh <old.fpk> <new.fpk>}"
SIM="${AIRCONNECT_SIM_ROOT:-/vol1/@apphome/airconnect/data/fpk-upgrade}"
PORT="${AIRCONNECT_SIM_PORT:-18998}"

fail=0
ok()  { echo "  ✅ $1"; }
bad() { echo "  ❌ $1"; fail=1; }

cleanup() {
    TRIM_APPDEST="$SIM/target" TRIM_PKGVAR="$SIM/var" TRIM_PKGHOME="$SIM/home" \
        TRIM_PKGETC="$SIM/etc" TRIM_SERVICE_PORT="$PORT" \
           TRIM_USERNAME=root TRIM_RUN_USERNAME=root \
        /bin/bash "$SIM/cmd/main" stop >/dev/null 2>&1 || true
    for f in "$SIM/var/airupnp.pid" "$SIM/var/aircast.pid"; do
        [ -r "$f" ] || continue
        pid="$(head -1 "$f" | tr -d '[:space:]')"
        [ -n "$pid" ] && kill -KILL "$pid" 2>/dev/null
    done
}
trap cleanup EXIT

unpack_fresh() {
    chmod -R u+rwX "$SIM" 2>/dev/null || true
    rm -rf "$SIM"
    mkdir -p "$SIM/work" "$SIM/target" "$SIM/var" "$SIM/etc" "$SIM/home"
    tar -xzf "$1" -C "$SIM/work"
    tar -xzf "$SIM/work/app.tgz" -C "$SIM/target"
    # 真实布局：cmd/ 是 target 的兄弟目录（/var/apps/<app>/cmd vs /var/apps/<app>/target）
    rm -rf "$SIM/cmd"
    cp -r "$SIM/work/cmd" "$SIM/cmd"
    install_stubs
}

# 模拟飞牛的升级解包：**只替换程序目录**，var / etc / home 原样保留
upgrade_payload() {
    rm -rf "$SIM/work-new"
    mkdir -p "$SIM/work-new"
    tar -xzf "$1" -C "$SIM/work-new"
    rm -rf "$SIM/target"
    mkdir -p "$SIM/target"
    tar -xzf "$SIM/work-new/app.tgz" -C "$SIM/target"
    rm -rf "$SIM/cmd"
    cp -r "$SIM/work-new/cmd" "$SIM/cmd"
    install_stubs
}

install_stubs() {
    # 假二进制：不向局域网广播测试用的 AirPlay 设备
    for name in airupnp aircast; do
        cat > "$SIM/target/bin/$name" <<'STUB'
#!/bin/sh
# 假桥接二进制：
#   * -h 打印版本号（管理服务要拿它显示版本）
#   * 模仿 -I 的自动保存：把发现的设备写进 -x 指定的配置文件里
# 只认**独立的** -h：写成 `case "$*" in *-h*)` 会误判 —— 临时目录名里一旦
# 出现 "-h"（CI 上就抽到了 /tmp/airconnect-e2e-h2qu76sj），假二进制会以为在问
# 版本、打印一行就退出，监管进程便无限重启它，测试全线崩。
for a in "$@"; do
    if [ "$a" = "-h" ]; then echo "v1.12.4 (simulated stub)"; exit 0; fi
done
XML=""; prev=""
for a in "$@"; do [ "$prev" = "-x" ] && XML="$a"; prev="$a"; done
echo "[00:00:00.100] main:1407 Starting $(basename "$0") version: v1.12.4 (stub)"
echo "[00:00:00.300] AddMRDevice:1038 [0x0]: adding renderer (模拟音箱) with mac BBBB72C30EDB"
if [ -n "$XML" ] && [ -f "$XML" ]; then
    python3 - "$XML" <<'PYSTUB' 2>/dev/null
import sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
if "uuid:sim-stub-device" not in text:
    block = ("<device>\n<udn>uuid:sim-stub-device</udn>\n<name>模拟音箱+</name>\n"
             "<mac>bb:bb:db:0e:c3:72</mac>\n<enabled>1</enabled>\n</device>\n")
    open(path, "w", encoding="utf-8").write(text.replace("</airupnp>", block + "</airupnp>"))
PYSTUB
fi
while true; do sleep 1; done
STUB
        chmod 755 "$SIM/target/bin/$name"
    done
}

export_sim_env() {
    export TRIM_APPDEST="$SIM/target" TRIM_PKGVAR="$SIM/var" TRIM_PKGHOME="$SIM/home" \
           TRIM_PKGETC="$SIM/etc" TRIM_SERVICE_PORT="$PORT" \
           TRIM_USERNAME=root TRIM_RUN_USERNAME=root \
           TRIM_TEMP_LOGFILE="$SIM/install-error.log" \
           GATEWAY_SOCKET="$SIM/target/airconnect.sock" GATEWAY_PREFIX="/app/airconnect"
    export PATH="/var/apps/python312/target/bin:$PATH"
    export wizard_mode="upnp" wizard_name_suffix="+" wizard_log_level="info" wizard_data_action="keep"
}

health() { curl -s -m 8 "http://127.0.0.1:$PORT/api/health"; }
server_pid() { head -1 "$SIM/var/server.pid" 2>/dev/null | tr -d '[:space:]'; }

echo "▶ 场景 1：安装旧版本并启动"
unpack_fresh "$OLD_FPK"
OLD_VER="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "$SIM/work/manifest" | tr -d '[:space:]')"
export_sim_env
TRIM_APPVER="$OLD_VER"; export TRIM_APPVER
/bin/bash "$SIM/cmd/install_init" >/dev/null 2>&1 || true
/bin/bash "$SIM/cmd/install_callback" >/dev/null 2>&1
/bin/bash "$SIM/cmd/main" start >/dev/null 2>&1
HEALTH="$(health)"
echo "$HEALTH" | grep -q '"ok"' && ok "旧版本 $OLD_VER 已启动" || bad "旧版本启动失败：$HEALTH"
PID_OLD="$(server_pid)"
[ -n "$PID_OLD" ] && ok "服务 PID=$PID_OLD" || bad "读不到 server.pid"
ss -lntp 2>/dev/null | grep -q ":$PORT " && ok "端口 $PORT 已在监听" || bad "端口 $PORT 没在监听"

echo
echo "▶ 场景 2：升级到新版本（upgrade_init / upgrade_callback）"
# 只替换 app 内容与 cmd（模拟飞牛升级解包），保留 var/etc/home
upgrade_payload "$NEW_FPK"
NEW_VER="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "$SIM/work-new/manifest" | tr -d '[:space:]')"

# ★ 关键断言 1：upgrade_init 之后端口必须已经释放
/bin/bash "$SIM/cmd/upgrade_init" >/dev/null 2>&1
sleep 1
if ss -lntp 2>/dev/null | grep -q ":$PORT "; then
    bad "upgrade_init 之后端口 $PORT 仍被占用（旧进程没停 → 升级隐形故障）"
else
    ok "upgrade_init 已停服，端口 $PORT 释放"
fi
[ -f "$SIM/var/server.pid" ] && bad "PID 文件未被清理" || ok "PID 文件已清理"

echo
echo "▶ 场景 3：upgrade_callback 必须重新拉起服务"
TRIM_APPVER="$NEW_VER"; export TRIM_APPVER
if /bin/bash "$SIM/cmd/upgrade_callback" > "$SIM/upgrade.out" 2>&1; then
    ok "upgrade_callback 返回 0"
else
    bad "upgrade_callback 失败"; tail -20 "$SIM/upgrade.out"
fi

HEALTH="$(health)"
echo "$HEALTH" | grep -q '"ok"' && ok "升级后服务可用" || bad "升级后服务不可用：$HEALTH"
PID_NEW="$(server_pid)"
[ -n "$PID_NEW" ] && [ "$PID_NEW" != "$PID_OLD" ] \
    && ok "服务已换成新进程（$PID_OLD → $PID_NEW）" \
    || bad "PID 没有变化（$PID_OLD → $PID_NEW）：旧进程可能没被杀掉"

# ★ 关键断言 2：/api/health 必须报新版本号 —— 「升级到底生效没有」一眼可查
if echo "$HEALTH" | grep -q "\"version\": \"$NEW_VER\""; then
    ok "/api/health 报新版本 $NEW_VER"
else
    bad "/api/health 未报新版本（期望 $NEW_VER）：$HEALTH"
fi

echo
echo "▶ 场景 4：用户设置与设备列表跨升级保留"
python3 - "$SIM/etc/settings.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data.get("mode") == "upnp", data.get("mode")
assert data.get("name_suffix") == "+", data.get("name_suffix")
print("  settings.json 保持有效：mode=%s name_suffix=%r" % (data["mode"], data["name_suffix"]))
PY
[ $? -eq 0 ] && ok "配置未损坏" || bad "配置损坏"
[ -d "$SIM/home" ] && ok "应用家目录保留" || bad "应用家目录丢失"

echo
echo "▶ 场景 5：升级后重新安装依赖失败也不能把应用升级死"
# 模拟 settings_cli 校验失败：把 settings.json 写成非法值（mode 不合法），
# 此时 config 迁移必须保留原文件，且 upgrade_callback 仍要能把服务拉起来。
python3 - "$SIM/etc/settings.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["mode"] = "bogus"
json.dump(data, open(path, "w", encoding="utf-8"))
PY
TRIM_APPVER="$NEW_VER" /bin/bash "$SIM/cmd/main" stop >/dev/null 2>&1
TRIM_APPVER="$NEW_VER" /bin/bash "$SIM/cmd/upgrade_callback" >/dev/null 2>&1
HEALTH="$(health)"
echo "$HEALTH" | grep -q '"ok"' \
    && ok "配置被写坏后依然能启动（回落到默认值）" \
    || bad "配置写坏后应用起不来：$HEALTH"

echo
if [ "$fail" = "0" ]; then
    echo "✅ 升级路径全部断言通过"
else
    echo "❌ 升级路径存在失败项"
fi
exit "$fail"
