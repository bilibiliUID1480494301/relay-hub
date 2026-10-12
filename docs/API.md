# relayhub Python API 参考

> 签名由 `inspect.signature` 从实际包自动生成 + 人工润色，随 `hubrelay` 发布版同步更新。

## 建站（relayhub.api）

- `Station(port: 'int' = 8799, host: 'str' = '127.0.0.1', master_key: 'str' = 'rh_local_dev', name: 'str' = 'relay-hub', home: 'str | Path | None' = None) -> 'None'`
  - `Station.serve(self, background: 'bool' = False, discover: 'bool' = False, verbose: 'bool' = False) -> 'str'`
## 客户端（relayhub.client）

### class StationClient(base_url: 'str', token: 'str' = '', *, timeout: 'float' = 60.0, e2e: 'str' = 'auto', plugin_id: 'str' = 'hubrelay-client') -> 'None'
> 一台中转站的客户端句柄。
- `StationClient.models(self) -> 'dict[str, Any]'`
## 端到端加密（relayhub.gateway.e2e，可选依赖 hubrelay[e2e]）

- `e2e.seal_envelope(server_public: 'str | bytes', key_id: 'str', payload: 'dict[str, Any]', credential: 'str', *, ts: 'int | None' = None, nonce: 'bytes | None' = None) -> 'dict[str, Any]'`
- `e2e.open_envelope(identity: 'E2eIdentity', envelope: 'dict[str, Any]', credential: 'str', replay: 'ReplayGuard') -> 'dict[str, Any]'`
- `e2e.derive_key(shared: 'bytes', credential: 'str') -> 'bytes'`
- `e2e.load_or_create(path: 'Path') -> 'E2eIdentity'`
### class E2eIdentity(key_id: 'str', private: 'Any', public: 'bytes') -> 'None'
> 站点的 X25519 静态身份。私钥只在内存，落盘走 secretbox。
- `E2eIdentity.__init__(self, key_id: 'str', private: 'Any', public: 'bytes') -> 'None'`
### class ReplayGuard(window: 'float' = 120.0) -> 'None'
> (credential, key_id, nonce) 短期去重。进程内存态：重启清零，
- `ReplayGuard.__init__(self, window: 'float' = 120.0) -> 'None'`
### class E2eError(...)
> 信封不可用/不合法。message 面向客户端，可直接回 4xx。
- `E2eError.__init__(self, /, *args, **kwargs)`
## 工具（relayhub.api）

- `api.doctor(*, port: 'int' = 8799, scan_timeout: 'float' = 1.5) -> 'list[dict[str, Any]]'`
- `api.quickstart(base_url: 'str', api_key: 'str' = '', models: 'Sequence[str]' = (), port: 'int' = 8799, master_key: 'str' = 'rh_local_dev', background: 'bool' = True) -> 'tuple[Station, str]'`
- `api.list_providers() -> 'dict[str, str]'`
- `api.scan_local(host: 'str' = '127.0.0.1', ports: 'Sequence[int] | None' = None, timeout: 'float' = 2.0) -> 'list[LocalServer]'`
## 数据类型

### class TokenIssued(name: 'str', plaintext: 'str', models: 'list[str]', rpm: 'int', daily_requests: 'int', tokens_file: 'str') -> None
> Receipt of one create_token call / 一次 create_token 的回执。
### class UpstreamAdded(label: 'str', base_url: 'str', models: 'list[str]', pool_file: 'str') -> None
> Receipt of one add_upstream call / 一次 add_upstream 的回执。
## 渠道池（relayhub.gateway.pool）

### class UpstreamKey(key_id: 'str', label: 'str', base_url: 'str', api_key: 'str', protocol: 'str' = 'anthropic-messages', models: 'tuple[str, ...]' = (), model_windows: 'dict[str, int]' = <factory>, model_mapping: 'dict[str, str]' = <factory>, priority: 'int' = 0, weight: 'int' = 1, group: 'str' = 'default', cost_multiplier: 'float' = 1.0, price_in_per_1k: 'float' = 0.0, price_out_per_1k: 'float' = 0.0, enabled: 'bool' = True, note: 'str' = '', auth_mode: 'str' = 'auto', extra_headers: 'dict[str, str]' = <factory>, credits: 'int' = -1, credits_updated_at: 'float' = 0.0, cooldown_tier: 'str' = '', consecutive_failures: 'int' = 0, disabled_until: 'float' = 0.0, last_error: 'str' = '', usage: 'Usage' = <factory>) -> None
> UpstreamKey(key_id: 'str', label: 'str', base_url: 'str', api_key: 'str', protocol: 'str' = 'anthropic-messages', models: 'tuple[str, ...]' = (), model_windows: 'dict[str, int]' = <factory>, model_mapping: 'dict[str, str]' = <factory>, priority: 'int' = 0, weight: 'int' = 1, group: 'str' = 'default', cost_multiplier: 'float' = 1.0, price_in_per_1k: 'float' = 0.0, price_out_per_1k: 'float' = 0.0, enabled: 'bool' = True, note: 'str' = '', auth_mode: 'str' = 'auto', extra_headers: 'dict[str, str]' = <factory>, credits: 'int' = -1, credits_updated_at: 'float' = 0.0, cooldown_tier: 'str' = '', consecutive_failures: 'int' = 0, disabled_until: 'float' = 0.0, last_error: 'str' = '', usage: 'Usage' = <factory>)
- `UpstreamKey.supports(self, model: 'str') -> 'bool'`
- `UpstreamKey.public_models(self) -> 'tuple[str, ...]'`
- `UpstreamKey.map_to_upstream(self, model: 'str') -> 'str'`
- `UpstreamKey.is_available(self, now: 'float') -> 'bool'`
- `UpstreamKey.upstream_cost(self, tokens_in: 'int', tokens_out: 'int') -> 'int'`
- `UpstreamKey.to_dict(self) -> 'dict'`
### class KeyPool(keys: 'Sequence[UpstreamKey]', *, strategy: 'str' = 'round_robin', failure_threshold: 'int' = 3, cooldown_tiers: 'dict[str, float] | None' = None, retries: 'int' = 1, config_epoch: 'int' = 0, clock: 'Callable[[], float]' = <built-in function time>) -> 'None'
> 一组上游 Key。候选排序 + 熔断 + 用量记账。
- `KeyPool.candidates(self, model: 'str', strategy: 'str | None' = None, group: 'str | None' = None) -> 'list[UpstreamKey]'`
- `KeyPool.all_models(self, group: 'str | None' = None) -> 'list[str]'`
- `KeyPool.groups(self) -> 'list[str]'`
- `KeyPool.add(self, key: 'UpstreamKey') -> 'UpstreamKey'`
- `KeyPool.remove(self, identifier: 'str') -> 'bool'`
- `KeyPool.set_enabled(self, identifier: 'str', enabled: 'bool') -> 'bool'`
- `KeyPool.report_success(self, key: 'UpstreamKey', tokens_in: 'int' = 0, tokens_out: 'int' = 0, cache_read: 'int' = 0, cache_creation: 'int' = 0) -> 'None'`
- `KeyPool.report_failure(self, key: 'UpstreamKey', error: 'object', *, trip: 'bool' = True) -> 'None'`
- `KeyPool.stats(self) -> 'dict'`
- `KeyPool.save(self, path: 'Path') -> 'None'`
- `pool.classify_failure(status: 'int', message: 'str') -> 'str | None'`
- `pool.key_from_spec(raw: 'dict') -> 'UpstreamKey'`
## 下游令牌（relayhub.gateway.tokens）

### class DownstreamToken(token_id: 'str', name: 'str', token: 'str', enabled: 'bool' = True, models: 'tuple[str, ...]' = (), scope: 'str' = 'normal', rpm: 'int' = 0, daily_requests: 'int' = 0, expires_at: 'float' = 0.0, user_id: 'str' = '', group: 'str' = 'default', channel_group: 'str' = '', token_hash: 'str' = '', token_hint: 'str' = '', note: 'str' = '', created_at: 'float' = <factory>, usage: 'TokenUsage' = <factory>) -> None
> DownstreamToken(token_id: 'str', name: 'str', token: 'str', enabled: 'bool' = True, models: 'tuple[str, ...]' = (), scope: 'str' = 'normal', rpm: 'int' = 0, daily_requests: 'int' = 0, expires_at: 'float' = 0.0, user_id: 'str' = '', group: 'str' = 'default', channel_group: 'str' = '', token_hash: 'str' = '', token_hint: 'str' = '', note: 'str' = '', created_at: 'float' = <factory>, usage: 'TokenUsage' = <factory>)
- `DownstreamToken.allows(self, model: 'str') -> 'bool'`
- `DownstreamToken.is_expired(self, now: 'float | None' = None) -> 'bool'`
- `DownstreamToken.to_dict(self) -> 'dict'`
### class TokenPool(tokens: 'Sequence[DownstreamToken]' = ()) -> 'None'
> 一组下游令牌 + 鉴权判定 + 按令牌记账。纯数据，不做热加载（那是 TokenStore 的事）。
- `TokenPool.add(self, token: 'DownstreamToken') -> 'DownstreamToken'`
- `TokenPool.remove(self, identifier: 'str') -> 'bool'`
- `TokenPool.find(self, secret: 'str') -> 'DownstreamToken | None'`
- `TokenPool.set_enabled(self, identifier: 'str', enabled: 'bool') -> 'bool'`
- `TokenPool.report(self, token: 'DownstreamToken', ok: 'bool', tokens_in: 'int' = 0, tokens_out: 'int' = 0) -> 'None'`
- `TokenPool.stats(self) -> 'dict'`
- `TokenPool.save(self, path: 'Path') -> 'None'`
## 用户与额度（relayhub.gateway.users）

### class UserPool(users: 'list[User] | None' = None, *, allow_register: 'bool' = False, default_quota: 'int' = 1000, price_in_per_1k: 'float' = 2.0, price_out_per_1k: 'float' = 6.0, groups: 'dict[str, float] | None' = None, peak_hours: 'list[int] | None' = None, peak_multiplier: 'float' = 1.0) -> 'None'
> 一组用户 + 鉴权 + 计费规则。纯数据；热加载在 UserStore。
- `UserPool.register(self, username: 'str', password: 'str', *, role: 'str' = 'user') -> 'User'`
- `UserPool.verify(self, username: 'str', password: 'str') -> 'User | None'`
- `UserPool.cost_of(self, user: 'User', tokens_in: 'int', tokens_out: 'int', ts: 'float | None' = None) -> 'int'`
- `UserPool.time_multiplier(self, ts: 'float | None' = None) -> 'float'`
- `UserPool.deduct(self, user_id: 'str', cost: 'int') -> 'bool'`
- `UserPool.pre_consume(self, user_id: 'str', estimated: 'int') -> 'bool'`
- `UserPool.settle(self, user_id: 'str', pre: 'int', actual: 'int') -> 'None'`
- `UserPool.save(self, path: 'Path') -> 'None'`
### class RedeemStore(path: 'Path') -> 'None'
> 兑换码：一次性核销。codes.json 与 users.json 同目录。
- `RedeemStore.generate(self, count: 'int', credits: 'int', *, prefix: 'str' = 'rhx') -> 'list[RedeemCode]'`
- `RedeemStore.redeem(self, code: 'str', user_id: 'str') -> 'int'`
- `RedeemStore.list(self) -> 'list[RedeemCode]'`
