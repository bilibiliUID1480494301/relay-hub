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
