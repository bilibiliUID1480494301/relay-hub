"""发布签名公钥（随包分发，所有站长共用同一把）。

私钥**不在仓库里**，由发布者离线保管。公钥写死在这里的用意是：
任何站长装到包后都能验证「这个 wheel 确实由 relay-hub 官方发布」，
而不是只信任 TLS——那样谁劫持 CDN 都能投毒。
"""

PUBLIC_KEY_B64 = "UalueWu9iiKHBRQnANytr4J/MDTjlt3NINOjWpdQd0g="
