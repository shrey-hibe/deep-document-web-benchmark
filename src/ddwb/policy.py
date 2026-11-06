"""The harness policy (`policy.toml`): thresholds the pipeline reads, the sub-agents of the Claude Code plugin, and what
a run may do on its own. `python -m ddwb.policy agents <dir>` writes the plugin's agent definitions from it."""

from __future__ import annotations

import sys
import tomllib
from functools import cache
from pathlib import Path

POLICY = Path(__file__).with_name("policy.toml")
SERVER = "public-records"  # the MCP server's name in the plugin (.mcp.json)
PLUGIN = "public-records"  # the plugin's name: Claude Code names its tools mcp__plugin_<plugin>_<server>__<nav step>


@cache
def policy() -> dict:
    return tomllib.loads(POLICY.read_text())


def decide() -> dict:
    return policy()["decide"]


def profile() -> dict[str, dict]:
    """The per-body knobs: {name: {default, low, high, what}}."""
    return policy()["profile"]


def audit() -> dict:
    return policy()["audit"]


def agent_markdown(a: dict, autonomy: dict) -> str:
    """A Claude Code sub-agent definition: frontmatter (name, description, model, tools) and its brief."""
    tools = ", ".join([f"mcp__plugin_{PLUGIN}_{SERVER}__{t}" for t in a["tools"]] + a.get("own_tools", []))
    lines = ["---", f"name: {a['name']}", f"description: {a['stage'][0].upper() + a['stage'][1:]}. Part of the public-records "
             "plugin; started by the public-records skill with a session id.", f"model: {a['model']}", f"tools: {tools}", "---", "",
             f"You {a['stage']}, for one public body's session, using only your tools.", "",
             f"- Budget: the run's `{a['budget']}` cap; the tools stop at it.",
             f"- Stop when: {a['stop']}.", f"- Then hand over {a['hands_over']}.",
             "- A tool that fails tells you why; a failure is never \"nothing there\". Try another way or mark it unreachable "
             "with the reason.", "", "You may: " + "; ".join(autonomy["free"]) + ".",
             "Ask first: " + "; ".join(autonomy["needs_approval"]) + ".", "Never: " + "; ".join(autonomy["never"]) + "."]
    return "\n".join(lines) + "\n"


def write_agents(out: Path) -> list[Path]:
    p = policy()
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for a in p["agents"]:
        f = out / f"{a['name']}.md"
        f.write_text(agent_markdown(a, p["autonomy"]))
        written.append(f)
    return written


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "agents":
        raise SystemExit("usage: python -m ddwb.policy agents <dir>")
    for f in write_agents(Path(sys.argv[2])):
        print(f)
