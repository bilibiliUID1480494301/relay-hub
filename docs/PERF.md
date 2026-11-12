# 性能基线与对比方法

## 我们自己的基线（Windows 10 / i7 / Python 3.11，演示池，不走网络上游）

```
python scripts/bench.py --threads 16 --requests 40
16x40: 1152 req/s  p50=4.7ms  p95=16.4ms  max=34.0ms

python scripts/bench.py --threads 32 --requests 40
32x40: 1031 req/s  p50=7.2ms  p95=36.6ms  max=47.9ms
```

这是**网关自身开销**（解析、鉴权、协议翻译、记账），不含上游延迟。

## 与 new-api 对比的正确做法

我（AI）没有在本地跑过 new-api，所以不提供「我们 vs 它」的数字——
那是编的。要比就同机同条件跑：

1. 同一台机器、同一份请求体（`max_tokens=8`，单轮对话，非流式）
2. 同样并发（先 16 再 32），同样的 keep-alive 行为
3. 上游都指向同一个假上游（`scripts/fake_upstream.py`），避免真实上游抖动
4. 每档跑 3 次取中位数

## 已知的结构差异（不是调参能抹平的）

| 维度 | relay-hub | new-api |
|---|---|---|
| 运行时 | CPython + `ThreadingHTTPServer`（GIL） | Go（goroutine） |
| 并发模型 | 每连接一线程 | 每请求 goroutine |
| 序列化 | 标准库 `json` | 编译期结构体 |
| 优势 | 零依赖、可读、易审计 | 吞吐与内存占用 |

单机多核高并发下 new-api 大概率更快；relay-hub 的取舍是「单文件、无依赖、可审计」。
真要追吞吐，方向是换 WSGI/ASGI 服务器或改 Go，不是继续抠 Python 微优化。

## 已做的优化

- 令牌查找：O(n) 逐个 `compare_digest` → **哈希索引 O(1)**（保留一次恒定时间比较）
- SSE：每连接读盘 → **单例广播器**（N 客户端只读一次）
- 连接层：`request_queue_size=512`、`TCP_NODELAY`、keep-alive 连接池


## 0.12.10 的串行化点修复（第三方实测驱动的改动）

**现象**（第三方实测 0.12.5）：并发 8→128 涨 16 倍，RPS 卡在 ~1000 不动，p50 从 7.4ms
线性涨到 129.3ms，而 CPU 只用了 0.5 核——典型的「存在串行化点」。

**根因**：`router._persist()` 在**每个请求结束**都调用 `pool.save()`，而单次 `pool.save()`
实测 **3.25 ms**（全量序列化 + 密封 + fsync + `os.replace` + 全局锁）。1000 rps 意味着每秒
需要 3250 ms 的写盘时间，是单核的 3.25 倍，且全部串在同一把锁上。

**修法**：`persist.py` 延迟落盘——请求路径只置脏标记，后台线程每 150ms 合并写一次
（1000 rps 时写盘从 1000 次/秒降到 6~7 次/秒）。代价：最多丢 150ms 的**用量计数**；
计费凭证在 reqlog 追加日志里，不受影响；管理面读状态前会 `flush_all()`。

**默认参数调整**：`--ip-rpm` 默认 60 → **600**。60/分钟在局域网 NAT 下会把多设备正常流量
打成 429（第三方压测 c=8 时 17646/17647 全 429）。

## 建议参数档（4 vCPU）

```
--max-concurrency 64 --queue-size 512 --ip-rpm 0     # 压测/内网可信环境
--max-concurrency 32 --queue-size 256 --ip-rpm 600   # 公网生产
```

## 0.12.11：热路径三处（按审计优先级）

| 项 | 改动 | 影响 |
|---|---|---|
| **防自环误伤**（正确性） | `LoopGuard` 分两条阈值：带 `Via`/`X-Relay-Hub-Hops` 的请求仍按 3 拦，**直连客户端放宽到 32**（`RELAYHUB_LOOP_DIRECT_LIMIT`，0=只靠 Via 判环） | 8 个客户端并发同 prompt 不再被判环回 429；真环路仍被 Via 标记拦住 |
| 请求日志 | 常驻句柄 + 每 32 行 flush，chmod 只在打开时一次 | 去掉每请求 open/write/chmod 三条 syscall（审计实测关日志 +11%） |
| 号池热加载 | 指纹按时间窗缓存（`RELAYHUB_WATCH_INTERVAL`，默认 1s），窗口内不读盘 | 去掉每请求解封+解析号池（审计实测关 watch +27%） |

代价：热加载最多延迟 1s；请求日志崩溃最多丢 32 行明细（计费以号池/tokens 为准）。

## 0.12.11：热路径三处（按审计优先级）

| 项 | 改动 | 影响 |
|---|---|---|
| **防自环误伤**（正确性） | `LoopGuard` 分两条阈值：带 `Via`/`X-Relay-Hub-Hops` 的请求仍按 3 拦，**直连客户端放宽到 32**（`RELAYHUB_LOOP_DIRECT_LIMIT`，0=只靠 Via 判环） | 8 个客户端并发同 prompt 不再被判环回 429；真环路仍被 Via 标记拦住 |
| 请求日志 | 常驻句柄 + 每 32 行 flush，chmod 只在打开时一次 | 去掉每请求 open/write/chmod 三条 syscall（审计实测关日志 +11%） |
| 号池热加载 | 指纹按时间窗缓存（`RELAYHUB_WATCH_INTERVAL`，默认 1s），窗口内不读盘 | 去掉每请求解封+解析号池（审计实测关 watch +27%） |

代价：热加载最多延迟 1s；请求日志崩溃最多丢 32 行明细（计费以号池/tokens 为准）。
