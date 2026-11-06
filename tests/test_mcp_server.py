"""The MCP server exposes every nav step as a tool, built from the nav parser, and returns what the step prints."""

import asyncio

from ddwb import mcp_server


def test_every_nav_step_is_a_tool_and_a_refusal_comes_back_as_text(tmp_path, monkeypatch):
    from ddwb.deep import state as nav_state
    monkeypatch.setattr(nav_state, "NAV", tmp_path)
    names = set(mcp_server.steps())
    assert {"start", "fetch", "map", "read", "record", "skip", "next", "answer"} <= names
    server = mcp_server.build()
    listed = {t.name for t in asyncio.run(server.list_tools())}
    assert names <= listed
    out = mcp_server.call("next", "t9:test:cortez-co")  # no such session: the tool says so, the server survives
    assert out and "Traceback" not in out


def test_every_tool_says_what_it_does():
    assert all(len(text) > 30 for text in mcp_server.steps().values())


def test_settings_the_user_never_gave_are_unset():
    env = {"DDWB_BROWSER": "${user_config.browser}", "AI_GATEWAY_API_KEY": "", "DDWB_JUDGE": "off",
           "DDWB_DATA_DIR": "${CLAUDE_PLUGIN_DATA}/data", "HOME": "/home/x", "OTHER": "${KEEP}"}
    assert sorted(mcp_server.unset_placeholders(env)) == ["AI_GATEWAY_API_KEY", "DDWB_BROWSER", "DDWB_DATA_DIR"]
    assert env == {"DDWB_JUDGE": "off", "HOME": "/home/x", "OTHER": "${KEEP}"}


def test_the_plugin_files_agree():
    import json
    from pathlib import Path
    root = Path(__file__).parents[1]
    mcp = json.loads((root / ".mcp.json").read_text())["mcpServers"]["public-records"]
    config = json.loads((root / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
    used = {v.split("user_config.")[1].rstrip("}") for v in mcp["env"].values() if "user_config." in v}
    assert used == set(config) and "--extra" in mcp["args"] and "browser" in mcp["args"]
    assert all(v.startswith("${CLAUDE_PLUGIN_DATA}") for k, v in mcp["env"].items() if k.endswith("_DIR"))


def test_tools_take_the_steps_own_named_parameters(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "call", lambda step, session, argv=None: sent.append((step, session, argv)) or "ok")
    server = mcp_server.build()
    asyncio.run(server.call_tool("start", {"session": "r:a1:jeff-al", "query": "Jefferson County, AL"}))
    asyncio.run(server.call_tool("record", {"session": "r:a1:jeff-al", "passage": "Pabc1234", "type": "contract",
                                            "action": "approved", "what": "paving", "amount": "125000"}))
    asyncio.run(server.call_tool("skip", {"session": "r:a1:jeff-al", "targets": ["K1", "K2"], "reason": "other board", "rest": False}))
    asyncio.run(server.call_tool("portal", {"session": "r:a1:jeff-al", "action": "meetings", "target": "iqm2:x", "board": ""}))
    assert sent == [("start", "r:a1:jeff-al", ["--query", "Jefferson County, AL"]),
                    ("record", "r:a1:jeff-al", ["Pabc1234", "--type", "contract", "--action", "approved", "--amount", "125000",
                                                "--what", "paving"]),
                    ("skip", "r:a1:jeff-al", ["K1", "K2", "--reason", "other board"]),
                    ("portal", "r:a1:jeff-al", ["meetings", "iqm2:x", "--board"])]


def test_a_tool_states_what_it_needs():
    tools = {t.name: t.inputSchema for t in asyncio.run(mcp_server.build().list_tools())}
    assert set(tools["record"]["required"]) == {"session", "passage", "type", "action", "what"}
    assert tools["fetch"]["required"] == ["session", "target"] and "query" in tools["start"]["properties"]
