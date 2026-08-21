from host import needs_client_secret


def test_client_secret_mode_requires_a_secret() -> None:
    assert needs_client_secret("ClientSecret") is True
    assert needs_client_secret("client_secret") is True
    assert needs_client_secret("") is True


def test_keyless_modes_do_not_require_a_secret() -> None:
    assert needs_client_secret("FederatedCredentials") is False
    assert needs_client_secret("UserManagedIdentity") is False
