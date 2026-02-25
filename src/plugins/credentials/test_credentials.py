"""Plugin-specific tests for credential_vault. Use fixtures from plugins/conftest.py."""


def test_credential_vault_tools_load(load_plugin_tools_fixture):
    """This plugin exposes store_credential, get_credential, list_credentials, delete_credential."""
    load_plugin_tools = load_plugin_tools_fixture
    tools = load_plugin_tools("credentials")
    names = [t.name for t in tools]
    for expected in (
        "store_credential",
        "get_credential",
        "list_credentials",
        "delete_credential",
    ):
        assert expected in names, f"Expected tool {expected} in {names}"

