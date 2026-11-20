"""HTTP 接口：模型配置页面、MCP 端点与同源防护。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api.main import create_app
from packages.application.runtime import PlatformRuntime
from packages.common.config import Settings
from tests.helpers import GOOD_PLAN, TEST_KEY, FakeModel


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    runtime = PlatformRuntime(
        Settings(_env_file=None, llm_api_key=SecretStr(TEST_KEY)),
        database_url="sqlite+aiosqlite:///" + (tmp_path / "api.db").as_posix(),
        transport=FakeModel([GOOD_PLAN]).transport(),
        env_path=tmp_path / ".env",
    )
    with TestClient(create_app(runtime)) as value:
        yield value


def settings_body(**values: object) -> dict[str, object]:
    return {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "max_tokens": 1024,
        "timeout_seconds": 60,
        **values,
    }


def test_model_settings_never_return_the_key(client: TestClient) -> None:
    response = client.get("/api/settings/model")
    assert response.status_code == 200
    data = response.json()
    assert data["key_configured"] and data["ready"]
    assert TEST_KEY not in response.text
    assert data["key_hint"] == TEST_KEY[:3] + "****" + TEST_KEY[-4:]


def test_save_settings_writes_env_and_applies(client: TestClient, tmp_path: Path) -> None:
    response = client.put(
        "/api/settings/model", json=settings_body(api_key="sk-new-key-0987654321", model="m2")
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "m2"
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-new-key-0987654321" in text and "PLATFORM_LLM_MODEL=m2" in text
    assert client.get("/api/config").json()["model"] == "m2"

    # 留空 → 沿用密钥；clear_key → 清除且不再就绪
    assert client.put("/api/settings/model", json=settings_body()).json()["key_configured"]
    cleared = client.put("/api/settings/model", json=settings_body(clear_key=True)).json()
    assert not cleared["key_configured"] and not cleared["ready"]


def test_invalid_settings_are_rejected(client: TestClient) -> None:
    assert (
        client.put(
            "/api/settings/model", json=settings_body(base_url="http://api.deepseek.com")
        ).status_code
        == 422
    )
    assert (
        client.put("/api/settings/model", json=settings_body(api_key="sk with space")).status_code
        == 400
    )


def test_connection_check_uses_models_endpoint(client: TestClient) -> None:
    response = client.post("/api/settings/model/test", json=settings_body())
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "models": ["deepseek-flash"], "model_listed": True}


def test_mcp_endpoint_speaks_json_rpc(client: TestClient) -> None:
    init = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert init.status_code == 200
    assert init.json()["result"]["serverInfo"]["name"] == "travel-tools"
    assert init.headers["MCP-Protocol-Version"]
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    call = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "verify_plan",
                "arguments": {"plan": GOOD_PLAN, "request": {"budget": 200}},
            },
        },
    )
    assert call.json()["result"]["structuredContent"]["total_cost"] == 130
    listed = client.get("/api/mcp/tools").json()
    assert len(listed["tools"]) == 4 and listed["calls"] >= 1


def test_cross_site_writes_are_blocked(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
