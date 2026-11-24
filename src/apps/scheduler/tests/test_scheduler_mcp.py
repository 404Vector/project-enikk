from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from enikk_contracts import routes
from enikk_contracts.errors import ErrorCode, ErrorResponse
from enikk_contracts.schedules import (
    Caller,
    RunList,
    RunsQuery,
    RunStatus,
    RunView,
    ScheduleList,
    ScheduleQuery,
)
from enikk_core_client import CoreClient, StaticTokenSource
from enikk_scheduler.app import HEALTH_PATH, MCP_PATH, create_app
from enikk_scheduler.caller import LEASE_HEADER, TASK_HEADER, MissingCallerError, caller_from

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

HOST = "testserver"
TASK = uuid.uuid4()
LEASE = uuid.uuid4()
CALLER_HEADERS = {TASK_HEADER: str(TASK), LEASE_HEADER: str(LEASE)}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> Iterator[TestClient]:
    core = CoreClient.connect(
        "http://core.test",
        StaticTokenSource(SecretStr("token")),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(create_app(core, allowed_hosts=(HOST,))) as client:
        yield client


@pytest.fixture
def requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def client(requests: list[httpx.Request]) -> Iterator[TestClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == routes.SCHEDULES_LIST:
            return httpx.Response(200, content=ScheduleList(schedules=()).model_dump_json())
        if request.url.path == routes.SCHEDULE_RUNS.format(name="daily"):
            now = datetime(2026, 11, 24, tzinfo=UTC)
            run = RunView(
                schedule_id=uuid.uuid4(),
                slot=now,
                status=RunStatus.SUCCEEDED,
                task_id=None,
                attempts=1,
                started_at=now,
                updated_at=now,
            )
            return httpx.Response(200, content=RunList(runs=(run,)).model_dump_json())
        error = ErrorResponse(code=ErrorCode.NOT_FOUND, message="no schedule: missing")
        return httpx.Response(404, content=error.model_dump_json())

    yield from _client(handler)


def _call(
    client: TestClient,
    tool: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    response = client.post(MCP_PATH, json=body, headers={**MCP_HEADERS, **(headers or {})})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


def test_health(client: TestClient) -> None:
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}


def test_health_answers_the_kubelet_on_the_pod_address(client: TestClient) -> None:
    response = client.get(HEALTH_PATH, headers={"host": "10.1.2.3:8080"})
    assert response.status_code == 200


def test_lists_only_the_read_tools(client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = client.post(MCP_PATH, json=body, headers=MCP_HEADERS)
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert set(tools) == {"list_schedules", "get_schedule", "list_runs"}
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools.values())


def test_passes_the_caller_to_core(client: TestClient, requests: list[httpx.Request]) -> None:
    result = _call(client, "list_schedules", {}, CALLER_HEADERS)
    assert not result.get("isError")
    assert result["structuredContent"] == {"schedules": []}
    (request,) = requests
    assert request.headers["authorization"] == "Bearer token"
    sent = ScheduleQuery.model_validate_json(request.content)
    assert sent.caller == Caller(task_id=TASK, lease_id=LEASE)


def test_sends_the_run_limit(client: TestClient, requests: list[httpx.Request]) -> None:
    result = _call(client, "list_runs", {"name": "daily", "limit": 5}, CALLER_HEADERS)
    assert result["structuredContent"]["runs"][0]["status"] == "succeeded"
    assert RunsQuery.model_validate_json(requests[0].content).limit == 5


def test_refuses_a_call_without_a_caller(client: TestClient, requests: list[httpx.Request]) -> None:
    result = _call(client, "list_schedules", {})
    assert result["isError"]
    assert TASK_HEADER in result["content"][0]["text"]
    assert requests == []


def test_reports_core_errors_as_tool_errors(client: TestClient) -> None:
    result = _call(client, "get_schedule", {"name": "missing"}, CALLER_HEADERS)
    assert result["isError"]
    assert "no schedule" in result["content"][0]["text"]


def test_rejects_an_unknown_host() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("core must not be called")

    for client in _client(handler):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = client.post(MCP_PATH, json=body, headers={**MCP_HEADERS, "host": "evil.example"})
        assert response.status_code == 421


@pytest.mark.parametrize(
    "headers",
    [None, {}, {TASK_HEADER: str(TASK)}, {TASK_HEADER: "x", LEASE_HEADER: str(LEASE)}],
)
def test_caller_needs_both_ids(headers: dict[str, str] | None) -> None:
    with pytest.raises(MissingCallerError):
        caller_from(headers)


def test_caller_headers_ignore_case() -> None:
    caller = caller_from({"X-Enikk-Task-Id": str(TASK), "X-ENIKK-LEASE-ID": str(LEASE)})
    assert caller == Caller(task_id=TASK, lease_id=LEASE)
