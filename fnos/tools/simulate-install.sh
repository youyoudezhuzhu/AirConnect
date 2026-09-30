#!/bin/bash
# 在没有应用中心的情况下，模拟一遍 fpk 的安装 → 启动 → 运行 → 停止流程。
#
# 用法：./tools/simulate-install.sh <app.fpk> [--real-binaries]
#
# 默认把 airupnp/aircast 换成**假二进制**：真二进制会往局域网广播 AirPlay
# 设备（会在你的 iPhone 上短暂出现一个重复音箱）。要连真二进制一起验证，
# 加 --real-binaries —— 它会真的做一次 SSDP/mDNS 发现。
set -u

FPK="${1:?用法: simulate-install.sh <app.fpk> [--real-binaries]}"
USE_REAL=0
[ "${2:-}" = "--real-binaries" ] && USE_REAL=1
FPK="$(cd "$(dirname "$FPK")" && pwd)/$(basename "$FPK")"
SIM="${AIRCONNECT_SIM_ROOT:-/vol1/@apphome/airconnect/data/fpk-sim}"
PORT="${AIRCONNECT_SIM_PORT:-18999}"

fail=0
ok()  { echo "  ✅ $1"; }
bad() { echo "  ❌ $1"; fail=1; }

echo "▶ 解析 fpk：$FPK"
MANIFEST="$(tar -xzOf "$FPK" manifest 2>/dev/null || tar -xzOf "$FPK" ./manifest 2>/dev/null)"
APPNAME="$(echo "$MANIFEST" | sed -n 's/^appname[[:space:]]*=[[:space:]]*//p' | tr -d '[:space:]')"
VERSION="$(echo "$MANIFEST" | sed -n 's/^version[[:space:]]*=[[:space:]]*//p' | tr -d '[:space:]')"
[ -n "$APPNAME" ] || { echo "❌ 从 fpk 里读不到 appname"; exit 1; }
echo "  appname=$APPNAME version=$VERSION"

echo "▶ 清理旧模拟环境：$SIM"
chmod -R u+rwX "$SIM" 2>/dev/null || true
rm -rf "$SIM"
mkdir -p "$SIM/work" "$SIM/target" "$SIM/var" "$SIM/etc" "$SIM/home"

echo "▶ 解包 fpk（两层：fpk → app.tgz）"
tar -xzf "$FPK" -C "$SIM/work"
[ -f "$SIM/work/app.tgz" ] || { echo "❌ fpk 里没有 app.tgz"; exit 1; }
tar -xzf "$SIM/work/app.tgz" -C "$SIM/target"

if [ "$USE_REAL" = "0" ]; then
    echo "▶ 替换为假桥接二进制（避免向局域网广播测试用 AirPlay 设备）"
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
else
    echo "▶ 使用真实桥接二进制（会做一次真实发现）"
fi

# install_init 会校验 manifest 里 install_dep_apps 声明的 python312 运行时。
# 飞牛上应用中心会先装好它；CI / 开发机上没有这个路径，会把「环境缺依赖」
# 误报成脚本缺陷。这里在**有权限时**造一个软链，让这条前置检查真的被跑到。
PY312="/var/apps/python312/target/bin/python3"
if [ ! -x "$PY312" ] && [ "$(id -u)" = "0" ]; then
    if mkdir -p "$(dirname "$PY312")" 2>/dev/null && ln -sf "$(command -v python3)" "$PY312" 2>/dev/null; then
        echo "▶ 非飞牛环境：已临时创建 python312 软链 $PY312（供 install_init 校验）"
    fi
fi

export TRIM_APPDEST="$SIM/target"
export TRIM_PKGVAR="$SIM/var"
export TRIM_PKGHOME="$SIM/home"
export TRIM_PKGETC="$SIM/etc"
export TRIM_TEMP_LOGFILE="$SIM/install-error.log"
export TRIM_SERVICE_PORT="$PORT"
export TRIM_APPVER="$VERSION"
# ⚠️ 必须**无条件**覆盖 TRIM_USERNAME：本机（DeepSeek Harness 本身也是一个飞牛应用）
# 的环境里已经带着 TRIM_USERNAME=deepseek.harness，用 ${TRIM_USERNAME:-root} 会把
# 模拟实例降权到那个用户；而 /vol1 卷根权限是 0000，该用户连目录都进不去，
# 表现成 socket bind Permission denied（技能总纲规则 #18）。
export TRIM_USERNAME="root"
export TRIM_RUN_USERNAME="root"
export TRIM_SYS_ARCH="${TRIM_SYS_ARCH:-x86_64}"
export GATEWAY_SOCKET="$SIM/target/$APPNAME.sock"
export GATEWAY_PREFIX="/app/$APPNAME"
export PATH="/var/apps/python312/target/bin:$PATH"

# 向导值（字段名 = wizard/*.json 里的 field）
export wizard_mode="upnp"
export wizard_name_suffix="·SIM"
export wizard_log_level="debug"
export wizard_data_action="keep"

# 真实布局：cmd/ 在 /var/apps/<appname>/cmd，TRIM_APPDEST 指向
# /vol<n>/@appcenter/<appname>。这里照着摆：$SIM/cmd 与 $SIM/target 是兄弟目录。
rm -rf "$SIM/cmd"
cp -r "$SIM/work/cmd" "$SIM/cmd"
CMD="$SIM/cmd"

echo
echo "▶ install_init"
if /bin/bash "$CMD/install_init"; then ok "install_init 通过"; else
    bad "install_init 失败"; [ -f "$TRIM_TEMP_LOGFILE" ] && cat "$TRIM_TEMP_LOGFILE"; fi

echo "▶ install_callback"
if /bin/bash "$CMD/install_callback" > "$SIM/install_callback.out" 2>&1; then ok "install_callback 通过"; else
    bad "install_callback 失败"; tail -20 "$SIM/install_callback.out"; fi

echo "▶ 安装后的配置"
[ -f "$SIM/etc/settings.json" ] && ok "settings.json 已生成" || bad "缺少 settings.json"
python3 - "$SIM/etc/settings.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print("  ❌ settings.json 不可解析:", exc); raise SystemExit(1)
print("  mode=%s name_suffix=%r main_log=%s" % (data.get("mode"), data.get("name_suffix"), data.get("main_log")))
ok = data.get("mode") == "upnp" and data.get("name_suffix") == "·SIM" and data.get("main_log") == "debug"
raise SystemExit(0 if ok else 1)
PY
[ $? -eq 0 ] && ok "向导值已写入（mode/name_suffix/main_log）" || bad "向导值没有写入"

echo "▶ cmd/main status（未启动应为 3）"
/bin/bash "$CMD/main" status; rc=$?
[ "$rc" = "3" ] && ok "status=3" || bad "status=$rc（期望 3）"

echo "▶ cmd/main start"
if /bin/bash "$CMD/main" start; then ok "start 通过"; else bad "start 失败"; fi

echo "▶ cmd/main status（应为 0）"
/bin/bash "$CMD/main" status; rc=$?
[ "$rc" = "0" ] && ok "status=0" || bad "status=$rc（期望 0）"

echo "▶ 健康检查 http://127.0.0.1:$PORT/api/health"
HEALTH="$(curl -s -m 8 "http://127.0.0.1:$PORT/api/health")"
echo "$HEALTH" | grep -q '"ok"' && ok "健康检查通过" || bad "健康检查失败：$HEALTH"

echo "▶ 首页 / 静态资源"
for path in / /css/app.css /js/app.js /images/icon_64.png; do
    code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$path")"
    [ "$code" = "200" ] && ok "GET $path → 200" || bad "GET $path → $code"
done

echo "▶ 飞牛统一网关（Unix 套接字 + /app/$APPNAME 前缀）"
[ -S "$SIM/target/$APPNAME.sock" ] && ok "套接字已创建 $(stat -c '%a' "$SIM/target/$APPNAME.sock")" || bad "套接字不存在"
code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' --unix-socket "$SIM/target/$APPNAME.sock" "http://localhost/app/$APPNAME/")"
[ "$code" = "200" ] && ok "网关首页 → 200" || bad "网关首页 → $code"
code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' --unix-socket "$SIM/target/$APPNAME.sock" "http://localhost/app/$APPNAME")"
[ "$code" = "307" ] && ok "裸前缀 → 307" || bad "裸前缀 → $code（期望 307）"

echo "▶ 设备列表"
sleep 2
curl -s -m 8 "http://127.0.0.1:$PORT/api/devices" | grep -q '模拟音箱' \
    && ok "已从 AirConnect 配置里读到设备" || bad "设备列表为空（AirConnect 应已自动保存配置）"

echo "▶ 应用设置向导（config_init / config_callback）"
wizard_name_suffix="·SIM2"
export wizard_name_suffix
/bin/bash "$CMD/config_init" >/dev/null 2>&1
if /bin/bash "$CMD/config_callback" >/dev/null 2>&1; then ok "config_callback 通过"; else bad "config_callback 失败"; fi
sleep 1
curl -s -m 8 "http://127.0.0.1:$PORT/api/health" | grep -q '"ok"' && ok "配置变更后服务仍在运行" || bad "配置变更后服务不可用"

echo "▶ cmd/main stop"
if /bin/bash "$CMD/main" stop; then ok "stop 通过"; else bad "stop 失败"; fi
/bin/bash "$CMD/main" status; rc=$?
[ "$rc" = "3" ] && ok "停止后 status=3" || bad "停止后 status=$rc（期望 3）"

echo "▶ 卸载流程（uninstall_init / uninstall_callback）"
/bin/bash "$CMD/uninstall_init" >/dev/null 2>&1 && ok "uninstall_init 通过" || bad "uninstall_init 失败"
wizard_data_action="keep"
export wizard_data_action
/bin/bash "$CMD/uninstall_callback" >/dev/null 2>&1
[ -f "$SIM/etc/settings.json" ] && ok "选择 keep 时配置被保留" || bad "配置被误删"

echo
echo "▶ 日志尾部（$SIM/var/main.log / server.log）"
tail -5 "$SIM/var/main.log" 2>/dev/null | sed 's/^/  /'
echo "  ---"
tail -5 "$SIM/var/server.log" 2>/dev/null | sed 's/^/  /'

echo
echo "▶ 残留进程检查"
LEFT="$(pgrep -f "$SIM/target/bin/airupnp" 2>/dev/null | wc -l)"
[ "$LEFT" = "0" ] && ok "没有遗留桥接进程" || bad "仍有 $LEFT 个桥接进程"
[ -S "$SIM/target/$APPNAME.sock" ] && bad "套接字文件未清理" || ok "套接字已清理"

echo
if [ "$fail" = "0" ]; then
    echo "✅ 安装模拟全部通过"
else
    echo "❌ 安装模拟存在失败项"
fi
exit "$fail"
