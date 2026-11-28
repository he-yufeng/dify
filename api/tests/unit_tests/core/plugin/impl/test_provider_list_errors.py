import json
from collections.abc import Callable

import httpx
import pytest
from flask import Flask
from flask_restx import Resource

from core.plugin.entities.plugin_daemon import PluginDaemonInnerError
from core.plugin.impl import base as client_module
from core.plugin.impl.exc import PluginDaemonInternalServerError
from core.plugin.impl.tool import PluginToolManager
from core.plugin.impl.trigger import PluginTriggerClient
from libs.external_api import ExternalApi


@pytest.mark.parametrize("kind", ["tools", "triggers"])
def test_list_daemon_error_reaches_api_error_handler(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    tenant_id = "tenant-1"
    requested: list[tuple[str, str]] = []
    errors: list[PluginDaemonInternalServerError] = []
    fetch: Callable[[str], object] = (
        PluginToolManager().fetch_tool_providers if kind == "tools" else PluginTriggerClient().fetch_trigger_providers
    )

    def daemon_reply(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.path))
        message = json.dumps({"error_type": "PluginDaemonInternalServerError", "message": "Internal Server Error"})
        return httpx.Response(200, json={"code": -500, "message": message, "data": None})

    app = Flask(__name__)
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    api = ExternalApi(app, register_default_root=False)

    @api.route("/provider-list")
    class ProviderList(Resource):
        def get(self) -> list[object]:
            try:
                fetch(tenant_id)
            except PluginDaemonInternalServerError as error:
                errors.append(error)
                raise
            return []

    with httpx.Client(transport=httpx.MockTransport(daemon_reply), trust_env=False) as http_client:
        monkeypatch.setattr(client_module, "_httpx_client", http_client)
        response = app.test_client().get("/provider-list")

    assert requested == [("GET", f"/plugin/{tenant_id}/management/{kind}")]
    assert response.status_code == 500, response.get_json()
    body = response.get_json()
    assert (body["status"], body["code"]) == (500, "unknown")
    assert [error.description for error in errors] == ["Internal Server Error"]


@pytest.fixture
def rejected_error_decoder(monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    from pydantic import ValidationError

    code = -500
    diagnostic = json.dumps({"error_type": "PluginDaemonInternalServerError", "message": "fixture diagnostic"})
    with pytest.raises(ValidationError) as rejected:
        client_module.PluginDaemonError.model_validate({})

    def reject_decoding(*_args: object, **_kwargs: object) -> object:
        raise rejected.value

    def daemon_reply(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            request=httpx.Request("GET", "https://daemon.invalid"),
            json={"code": code, "message": diagnostic, "data": None},
        )

    monkeypatch.setattr(client_module.PluginDaemonError, "model_validate_json", reject_decoding)
    monkeypatch.setattr(PluginToolManager, "_request", daemon_reply)
    return code, diagnostic


def test_daemon_decode_keeps_diagnostics(rejected_error_decoder: tuple[int, str]) -> None:
    with pytest.raises(PluginDaemonInternalServerError) as raised:
        PluginToolManager().fetch_tool_providers("tenant-1")

    assert raised.value.description == "Internal Server Error"
    cause = raised.value.__cause__
    assert isinstance(cause, PluginDaemonInnerError)
    assert (cause.code, cause.message) == rejected_error_decoder


def test_console_tool_provider_list_returns_generic_daemon_error(
    monkeypatch: pytest.MonkeyPatch, rejected_error_decoder: tuple[int, str]
) -> None:
    from inspect import unwrap
    from types import SimpleNamespace

    from controllers.console import bp
    from controllers.console.workspace.tool_providers import ToolCommonService, ToolProviderListApi

    console_get = unwrap(ToolProviderListApi.get)
    seen: list[tuple[str, str, object]] = []
    errors: list[PluginDaemonInternalServerError] = []

    def get_with_fixture_identity(resource: ToolProviderListApi) -> object:
        return console_get(resource, "tenant-1", SimpleNamespace(id="account-1"))

    def list_providers(account_id: str, tenant_id: str, provider_type: object) -> object:
        seen.append((account_id, tenant_id, provider_type))
        try:
            return PluginToolManager().fetch_tool_providers(tenant_id)
        except PluginDaemonInternalServerError as error:
            errors.append(error)
            raise

    monkeypatch.setattr(ToolProviderListApi, "get", get_with_fixture_identity)
    monkeypatch.setattr(ToolCommonService, "list_tool_providers", staticmethod(list_providers))
    app = Flask(__name__)
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    app.register_blueprint(bp)

    response = app.test_client().get("/console/api/workspaces/current/tool-providers")

    assert seen == [("account-1", "tenant-1", None)]
    assert len(errors) == 1
    cause = errors[0].__cause__
    assert isinstance(cause, PluginDaemonInnerError)
    assert (cause.code, cause.message) == rejected_error_decoder
    assert response.status_code == 500
    assert response.get_json() == {"code": "unknown", "message": "Internal Server Error", "status": 500}
    assert "fixture diagnostic" not in response.get_data(as_text=True)
