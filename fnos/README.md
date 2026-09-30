# AirConnect for fnOS（飞牛 OS）—— .fpk 打包工程

把 [philippe44/AirConnect](https://github.com/philippe44/AirConnect) 编译并封装成
飞牛 OS（fnOS）可以一键安装的原生应用包（`.fpk`）。

安装后，局域网里的 **DLNA / UPnP 音响、Sonos、Heos**（`airupnp`）和
**Chromecast / Google Cast 设备**（`aircast`）会以「虚拟 AirPlay 音箱」的形式出现在
iPhone / iPad / Mac 的 AirPlay 列表里，点一下就能播放。

> 本目录只包含**打包工程**（manifest / 生命周期脚本 / 管理服务 / Web UI / 构建脚本）。
> 上游 AirConnect 的 C 源码在仓库根目录，未做任何修改。

---

## 1. 产物

| 文件 | 说明 |
|---|---|
| `dist/AirConnect-<version>.fpk` | 飞牛应用安装包（应用中心「手动安装」可直接用） |

包内容（两层 tar，`fnpack` 规范）：

```text
AirConnect-1.12.4-1.fpk
├── manifest                 # INI：应用元数据（appname/version/platform/service_port…）
├── ICON.PNG / ICON_256.PNG  # 64 / 256 图标
├── cmd/                     # 9 个生命周期脚本
├── config/privilege|resource
├── wizard/install|config|uninstall
└── app.tgz
    ├── bin/airupnp          # 上游源码本地编译（x86_64）
    ├── bin/aircast
    ├── server/              # 管理服务（纯 Python 标准库）
    └── ui/                  # 管理界面（原生 HTML/CSS/JS）
```

## 2. 构建

```bash
# 首次构建（会初始化 git 子模块并从源码编译，约 1–3 分钟）
./fnos/scripts/build.sh

# 复用 fnos/app/bin 里已有的二进制，只重新打包（改 UI / 脚本时用）
./fnos/scripts/build.sh --skip-native

# 编译完全静态链接的版本（自包含，体积约 8 倍）
./fnos/scripts/build.sh --static

# 只编译、不打包（CI 用）
./fnos/scripts/build.sh --no-pack
```

构建依赖只有一个 x86_64 的 `gcc`（`build-essential`）：上游把 `common/*` 各平台的
静态库（`libraop.a`、`libpupnp.a`、`libcodecs.a`、`libmdns.a`、`libopenssl.a`）直接提交在
子模块仓库里，因此**不需要交叉工具链，也不需要先编 OpenSSL**。

`fnpack`（飞牛官方打包工具）已随仓库提交在 `fnos/fnpack/fnpack-linux-amd64`
—— 官方下载地址返回的是 HTML 页面而不是二进制，云编译不能靠 curl。

### 2.1 为什么默认动态链接

| | 动态（默认） | 静态（`--static`） |
|---|---|---|
| 单文件大小 | ~0.9 MB | ~7.4 MB |
| 运行期依赖 | `libc` / `libm`（`ldd` 实测），OpenSSL 由 `dlopen` 加载 | 无 |
| 最低要求 | glibc ≥ 2.34（Debian 12 / fnOS 1.1.8+） | 任意 Linux（内核即可） |
| OpenSSL | 跟随系统更新 | 冻结在包里 |

`manifest` 里 `os_min_version = 1.1.8`，即 fnOS 已经保证 glibc ≥ 2.34 且自带
`libssl.so.3`，所以默认用动态链接（体积小、安全更新跟得上）。目标系统 glibc 更旧时才需要
`--static`。

## 3. 安装与使用

1. 应用中心 → 手动安装 → 选择 `dist/AirConnect-<version>.fpk`。
   （`install_dep_apps = python312` 会自动带上 Python 3.12 依赖。）
2. 打开应用 → 「播放设备」里确认音响已被发现（没有就点「重新扫描」，最多等 30 秒）。
3. 不需要的播放器（电视、投影等）把「启用」关掉。
4. iPhone 控制中心 → AirPlay → 选择对应音箱名（默认是原名 + `+`）。
5. **Sonos / Heos 用户**：在「设置」里把延迟切到 `Sonos / Heos 预设 1000:2000`，
   否则会卡顿或不出声。

管理界面同时可以通过 `http://<NAS_IP>:18888` 直连。

## 4. 架构

```text
iPhone / iPad / Mac
        │ AirPlay（mDNS 广播 _raop._tcp，AirConnect 自己实现）
        ▼
┌──────────────────────────────────────────────────────────┐
│ 管理服务 main.py（airconnect 用户，Python 3.12 标准库）    │
│   ├─ HTTP/REST + 静态 UI（TCP 18888 + 网关 Unix 套接字）   │
│   └─ 进程监管（崩溃退避重启、日志采集与原地轮转）           │
│        ├─ airupnp  → DLNA / UPnP / Sonos / Heos          │
│        └─ aircast  → Chromecast / Google Cast            │
└──────────────────────────────────────────────────────────┘
        │ UPnP AVTransport / Cast 协议
        ▼
     真实音响
```

| 文件 | 职责 |
|---|---|
| `app/server/main.py` | 入口：加载设置 → 生成 AirConnect 配置 → 拉起桥接 → 提供界面 → 监管 |
| `app/server/acpath.py` | 全部路径都来自 `TRIM_*` 环境变量，绝不硬编码 |
| `app/server/acconf.py` | 设置模型 + 校验 + 生成 / 解析 AirConnect 的 `airupnp.xml`、`aircast.xml` |
| `app/server/acproc.py` | airupnp / aircast 进程监管、日志采集、崩溃退避、设备发现解析 |
| `app/server/aclog.py` | **原地**日志轮转（5 MiB 上限，保留尾部 1 MiB）+ 尾部读取 |
| `app/server/acweb.py` | REST API、静态资源、飞牛统一网关（剥前缀 / 裸前缀 307） |
| `app/server/settings_cli.py` | 生命周期脚本用的设置读写 CLI（安装/升级/向导） |

配置分区：

| 变量 | 路径 | 放什么 |
|---|---|---|
| `TRIM_APPDEST` | `/vol1/@appcenter/airconnect` | 二进制、服务代码、UI、网关套接字（升级被整体替换） |
| `TRIM_PKGVAR` | `/vol1/@appdata/airconnect` | 日志、PID |
| `TRIM_PKGETC` | `/vol1/@appconf/airconnect` | `settings.json`（本应用的设置） |
| `TRIM_PKGHOME` | `/vol1/@apphome/airconnect` | `airupnp.xml` / `aircast.xml`（属于用户数据） |
| `/var/apps/airconnect/cmd` | —— | 生命周期脚本（**不在 `TRIM_APPDEST` 下**） |

## 5. 踩过的坑（都写进了代码注释，别再踩）

1. **`cmd/` 不在 `TRIM_APPDEST` 里。** 飞牛把 fpk 的 `cmd/` 放到
   `/var/apps/<appname>/cmd`，而 `TRIM_APPDEST` 指向
   `/vol<n>/@appcenter/<appname>`（`/var/apps/<app>/target` 是指向它的软链）。
   用 `${TRIM_APPDEST}/cmd/main` 会**永远找不到脚本** → `upgrade_init` 静默不执行 →
   旧进程占着端口 → 「升级了但功能没变」。本工程改用 `$0` 反推脚本目录。
2. **`airupnp -h` 的退出码是 1**（打印用法后 `exit(1)`），
   配合 `set -o pipefail` 会把构建脚本直接带崩。判定版本必须看输出，不看退出码。
3. **`HOME` 必须无条件覆盖成 `$TRIM_PKGHOME`。** 飞牛已注入 `HOME=/root`，
   `${HOME:-...}` 不会生效。
4. **日志轮转只能原地重写，不能 `mv`/`rename`。** 子进程的 stdout/stderr 以
   `O_APPEND` 持有 fd，改名后它们继续写进「已改名的旧 inode」。
   `tools/test-logrotate.sh` 用 inode 不变 + 轮转后同 fd 仍可写入两条断言守住它。
5. **单行超长的日志**不能因为 `awk 'NR>1'` 丢掉首行而被清空，必须退化为「保留原始尾部」。
6. **`threading.Thread` 的子类不能把属性命名为 `_stop`** —— 会覆盖 `Thread._stop()`，
   `join()` 时抛 `TypeError: 'Event' object is not callable`，进程以非 0 退出。
7. **`fnpack build` 打包失败时仍返回退出码 0**，必须解析输出里的
   `Packing successfully`，否则只会看到后续 `mv` 报的无关错误。
8. **不要在仓库目录里直接 `fnpack build`**：它会把被处理目录封成 `0000` ACL，
   连属主都删不掉。构建脚本一律用独立暂存目录（默认
   `/vol1/@apphome/airconnect/data/fpk-build`）。
9. **模拟/测试实例必须显式写死 `TRIM_USERNAME=root`**：本机（DeepSeek Harness
   自己也是飞牛应用）环境里带着 `TRIM_USERNAME=deepseek.harness`，
   `${TRIM_USERNAME:-root}` 会降权到该用户，而 `/vol1` 卷根权限是 `0000`，
   表现成网关套接字 `bind: Permission denied`。
10. **设备列表要先把 XML 合并回设置再写回**，否则「GET 设置 → 期间发现新设备 →
    POST 保存」会把刚发现的设备条目抹掉（`acconf.sync_devices` 里做过一次回归修复）。

## 6. 测试

```bash
cd fnos

# 单元测试（配置校验 / XML 生成解析 / 日志轮转）
python3 -m unittest discover -s tests -v

# 端到端：真起服务 + 假桥接进程，打 HTTP API（19 个用例）
python3 tests/test_service.py

# 日志轮转的 shell 断言（从 cmd/main 里提取真实函数跑）
./tools/test-logrotate.sh

# 模拟完整安装 → 启动 → 配置变更 → 停止 → 卸载
AIRCONNECT_SIM_ROOT=/tmp/fpk-sim ./tools/simulate-install.sh dist/AirConnect-<version>.fpk

# 升级路径（upgrade_init 必须真的停服、upgrade_callback 必须重新拉起）
AIRCONNECT_SIM_ROOT=/tmp/fpk-upgrade ./tools/test-upgrade.sh <old.fpk> <new.fpk>
```

`tools/test-logrotate.sh` 已用「故意把 `cat` 换成 `mv`」验证过确实会变红；
`tests/test_config.py::test_sync_devices_adds_new_ones` 是前述设备合并缺陷的回归用例。

## 7. 许可证

- 上游 AirConnect：**GPL-3.0**（见仓库根目录 `LICENSE`）。
- 本打包工程（manifest / cmd / app/server / app/ui / scripts / tools）：同样以
  **GPL-3.0** 分发，因为它与上游二进制构成同一作品的打包分发。
- 随包分发的 `airupnp` / `aircast` 二进制由本仓库中的上游源码编译而来，未修改。
