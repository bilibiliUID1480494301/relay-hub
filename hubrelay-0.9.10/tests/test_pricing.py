from relayhub import pricing
from relayhub.gateway.providers import _balance

SAMPLE = {
    "data": [
        {"model_name": "model-alpha", "model_ratio": 1.0, "completion_ratio": 2,
         "enable_groups": ["default"], "vendor_id": 1,
         "supported_endpoint_types": ["openai", "anthropic", "gemini"],
         "billing_mode": "tiered_expr", "billing_expr": "tier(\"base\", p * 10)"},
        {"model_name": "model-beta", "model_ratio": 1.0, "completion_ratio": 1,
         "enable_groups": ["default", "vip"], "vendor_id": 2,
         "supported_endpoint_types": ["openai", "openai-response", "anthropic", "gemini"],
         "billing_expr": "hour(\"UTC\") ? tier(\"base\", p * 2) : tier(\"base\", p * 1)"},
    ],
    "group_ratio": {"default": 0.3, "vip": 0.5},
    "supported_endpoint": {"anthropic": {}, "gemini": {}, "openai": {}, "openai-response": {}},
    "vendors": [{"id": 1, "name": "VendorA"}, {"id": 2, "name": "VendorB"}],
    "pricing_version": "v0",
}


def test_parse_pricing_summarises_models_groups_and_endpoints():
    out = pricing.parse(SAMPLE)
    assert out["count"] == 2
    assert [m["model"] for m in out["models"]] == ["model-alpha", "model-beta"]
    alpha = out["models"][0]
    assert alpha["vendor"] == "VendorA"
    assert alpha["groups"] == ["default"]
    assert "gemini" in alpha["endpoints"]
    assert out["group_ratio"]["vip"] == 0.5
    assert out["endpoints"] == ["anthropic", "gemini", "openai", "openai-response"]
    assert out["pricing_version"] == "v0"


def test_relayhub_balance_parses_quota_minus_used():
    bal = _balance("relayhub", {"quota": 1000, "used": 250})
    assert bal["amount"] == 750
    assert bal["currency"] == "points"


def test_openai_extras_survive_ir_round_trip():
    from relayhub.gateway.upstream import to_anthropic_request, to_openai_request

    source = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "p", "schema": {"type": "object"}}},
        "seed": 42,
        "presence_penalty": 0.4,
    }
    ir = to_anthropic_request(source)
    assert ir["_extras"]["seed"] == 42
    back = to_openai_request(ir)
    assert back["response_format"]["json_schema"]["name"] == "p"
    assert back["seed"] == 42
    assert back["presence_penalty"] == 0.4
    assert "top_k" not in back
