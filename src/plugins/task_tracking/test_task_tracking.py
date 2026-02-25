"""Plugin-specific tests for task_tracking. Use fixtures from plugins/conftest.py."""


def test_task_tracking_tools_load(load_plugin_tools_fixture):
    """This plugin exposes core task tools including follow-up scheduling."""
    load_plugin_tools = load_plugin_tools_fixture
    tools = load_plugin_tools("task_tracking")
    names = [t.name for t in tools]
    for expected in (
        "create_task",
        "schedule_followup_task",
        "list_tasks",
        "get_task",
        "update_task_status",
    ):
        assert expected in names, f"Expected tool {expected} in {names}"
