# 暂停 / 恢复 / 拖进度条延迟的实测定位（小爱音箱 S12）

2026-10-01 在真机上完成的一次定点测量，结论用来定 `latency` 的默认值。

**被测对象**：飞牛 NAS（AirConnect fpk 1.12.4-1）+ 小爱音箱 S12
（`modelName=S12`，`manufacturer=Mi, Inc.`，UDN `uuid:64f2215e-…`）+ iPhone AirPlay 2。

## 结论先行

| 段 | 实测值 | 能否调整 |
|---|---|---|
| AirConnect 的 RTP 缓冲 | **1750 ms** | ✅ 改 `<latency>` 的 rtp 段 |
| 音箱自身启动缓冲 | **约 1.0 – 1.7 s** | ❌ 固件行为 |
| AirConnect 的反应耗时 | **75 – 280 ms** | 已经不是瓶颈 |

体感的"约 5 秒" = 1.75s + 1.0~1.7s + 音箱音频通路的输出延迟。

## 1. 先排除"AirConnect 反应慢"

原始日志（`raop_log=warn`）里每次事件的处理耗时：

```
[23:55:08.334] HandleRAOP: Stop      → AVTStop
[23:55:08.611] uPNP stopped           ← 0.28 s
[23:55:13.366] AVTPlay
[23:55:13.861] uPNP playing           ← 0.50 s
```

再把 `raop_log` 提到 `info`，`raop_server.c:425` 会把**每一个 RTSP 方法**打出来，
于是能看到 `SetURI/Play` 之后音箱 **75 毫秒**就来拉 HTTP 了：

```
00:07:52.153  HandleRAOP: uPNP setURI http://<NAS-IP>:18362/stream-0.flac
00:07:52.153  AVTPlay
00:07:52.228  http_thread_func: got HTTP connection 20     ← +75 ms
```

所以瓶颈不在控制路径。

## 2. 那 1750 ms 从哪来

`common/libraop/src/raop_streamer.c`：

```c
// sync packet
case 0x54: {
    uint32_t rtp_now_latency = ntohl(*(uint32_t*)(pktp+4));
    uint32_t rtp_now         = ntohl(*(uint32_t*)(pktp+16));
    ...
    if (!ctx->latency) ctx->latency = rtp_now - rtp_now_latency;   // ★ 0 = 照用发送端宣告的
```

`<latency>` 的 rtp 段为 0 时，AirConnect 直接采用 **iOS 在同步包里宣告**的延迟。
把 `raop_log` 开到 `debug` 抓 `sync packet rtp_latency:%u rtp:%u` 两字段相减：

```
rtp_latency=440359407 rtp=440436582 → 差 77175 采样
77175 / 44100 = 1750 ms
```

这 1.75 秒会**在播放开始的瞬间整批灌给音箱**，于是它又同步变成音箱侧的缓冲。

## 3. 音箱那一侧

用独立探测器（0.5 s 轮询 `GetTransportState` / `GetPositionInfo`）看到：

```
00:07:52   PLAYING   rel=00:00:00
00:07:52   PLAYING   rel=00:00:00
00:07:56   PLAYING   rel=00:00:01     ← 前面约 4 秒 RelTime 纹丝不动
00:07:57   PLAYING   rel=00:00:02
```

`rel` 卡住的 4 秒 = AirConnect 扣着不发的 1.75 s + 音箱收到数据后自己还要攒的约 1~1.7 s。

## 4. 「暂停后第 3 秒又响一两秒」

关键证据：**iOS 暂停时什么都不发。** 整段 40 秒会话的 RTSP 命令只有：

```
00:07:52.019  FLUSH        ← 开播瞬间的一次
00:08:32.696  TEARDOWN     ← 这是"停止"，不是"暂停"
（中间全是 SET_PARAMETER 推封面/元数据）
```

上游 README 第 218–230 行也写明 iOS "simply stops pushing audio through the wire"。
AirConnect 收不到任何通知，只能把手里的音频放完 —— 残响就来自两段缓冲。

## 5. 两个受控实验（本地静音 WAV 流，不吵）

`/tmp/dlna_lab.py`：直接对音箱做 UPnP 控制，排除 AirPlay 变量。

**场景 A —— 播放中发 `AVTStop`：**

```
+17.3s  >>> 发送 AVTStop
+17.4s  STOPPED   rel=00:00:10        ← 0.1 秒内静音，没有余音
```

**场景 B —— 掐住 HTTP 流（不关连接，模拟断供）：**

```
+39.7s  >>> 掐住 HTTP
+40.3s  PLAYING   rel=00:00:11        ← 只多走约 1 秒
+51.9s  >>> 恢复发送
+51.9s  PLAYING   rel=00:00:11        ← 中间 12 秒完全冻住
+54.2s  PLAYING   rel=00:00:12        ← 恢复后自己接着走
```

两个结论：

1. `AVTStop` 能让 S12 **立刻**静音 ⇒ "检测断流就 Stop"理论上能把暂停做成瞬间生效；
2. 但 `AVTStop` 之后当前 URI 作废（该机型 `resume_requires_reannounce = True`），
   恢复必须重新 `SetURI + Play` 并重新缓冲 1~2 秒；而**不打断**时音箱会自己续上。

**所以没有引入"断流即 Stop"的改动** —— 那只是把"暂停慢"换成"恢复慢"，
而且 1 秒级的断流阈值还会被 WiFi 抖动误触发。省下的最多 0.25 秒，不值得。

## 6. 最终改动

`<latency>` 默认值 `0:0` → **`500:0`**（上游文档写明的抗抖动下限
"Below 500ms is not recommended"）。

* 预期收益：暂停 / 恢复 / seek 各少约 **1.25 秒**（尾巴从 ≈2.75 s 收到 ≈1.5 s）
* 安装向导与「应用设置」新增「播放延迟」选项；设置页新增「低延迟（推荐，500:0）」预设
* 想再压可以自己填 `300:0`（更激进，WiFi 差时不建议）
* `<latency>` 的 http 段（如 `500:1500`）会在开播时成批灌静音
  （`raop_streamer.c:831` / `:978`，`timeout=0` 连发），能再省约 0.5 秒，
  但会换来一段永久静音延迟，未作为默认

## 复现方法

```bash
# 1) 打开 RTSP 方法级日志（走应用自己的 API，会重启桥接）
curl -s -H 'Content-Type: application/json' -H 'X-Requested-With: XMLHttpRequest' \
     -X POST -d '{"raop_log":"debug"}' http://127.0.0.1:18888/api/settings

# 2) 独立探测音箱状态（不依赖 AirConnect 日志）
python3 fnos/tools/dlna_probe.py --seconds 300 --out /tmp/probe.log

# 3) iPhone 上播 20s → 暂停 15s → 恢复 → 拖进度条 → 停止
# 4) 对齐两边时间戳；完事把 raop_log 改回 warn
```
