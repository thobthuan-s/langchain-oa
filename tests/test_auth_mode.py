import pytest

from host import needs_client_secret, resolve_bind_host


def test_client_secret_mode_requires_a_secret() -> None:
    assert needs_client_secret("ClientSecret") is True
    assert needs_client_secret("client_secret") is True
    assert needs_client_secret("") is True


def test_keyless_modes_do_not_require_a_secret() -> None:
    assert needs_client_secret("FederatedCredentials") is False
    assert needs_client_secret("UserManagedIdentity") is False


def test_production_refuses_to_start_without_credentials() -> None:
    with pytest.raises(RuntimeError, match="without Activity Protocol credentials"):
        resolve_bind_host(False, False, "0.0.0.0", "Production")


def test_production_refuses_local_eval() -> None:
    with pytest.raises(RuntimeError, match="ENABLE_LOCAL_EVAL"):
        resolve_bind_host(True, True, "0.0.0.0", "production")


def test_unauthenticated_or_eval_modes_bind_to_loopback() -> None:
    assert resolve_bind_host(False, False, "0.0.0.0", "") == "127.0.0.1"
    assert resolve_bind_host(True, True, "0.0.0.0", "Development") == "127.0.0.1"
    assert resolve_bind_host(False, True, "127.0.0.1", "") == "127.0.0.1"


def test_authenticated_host_keeps_configured_bind_address() -> None:
    assert resolve_bind_host(True, False, "0.0.0.0", "Production") == "0.0.0.0"


def test_federated_credentials_configuration(monkeypatch) -> None:
    import host

    monkeypatch.setattr(host.settings, "service_connection_client_id", "blueprint-app")
    monkeypatch.setattr(host.settings, "service_connection_tenant_id", "tenant")
    monkeypatch.setattr(host.settings, "service_connection_client_secret", "should-not-be-used")
    monkeypatch.setattr(host.settings, "service_connection_auth_type", "FederatedCredentials")
    for name in ("CLIENT_ID", "TENANT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(host.settings, "service_connection_federated_client_id", "")
    assert host.LangchainOaHost.create_auth_configuration(object()) is None

    monkeypatch.setattr(host.settings, "service_connection_federated_client_id", "mi-client-id")
    config = host.LangchainOaHost.create_auth_configuration(object())

    assert config.AUTH_TYPE == "FederatedCredentials"
    assert config.FEDERATED_CLIENT_ID == "mi-client-id"
    assert config.CLIENT_SECRET is None
