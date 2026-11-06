"""The public-records MCP server: every nav step as one tool, for Claude Code (the plugin) or any MCP client.

Tools are built from the nav command line itself (`tools.parser()`), one per step (start, fetch, check, accept, map,
links, portal, read, passages, record, skip, next, status, answer, profile, ...), so a step added to nav is a tool here
with no other change, and a sub-agent can be given only its stage's steps (`policy.toml`). Each tool takes `session`
(`<run>:<arm>:<body>`) and the step's own parameters, named and typed as the command line defines them (`start` takes
`query` or `body_id`, `fetch` takes `target`, `record` takes `passage`, `type`, `action`, `what`, ...); `portal`, which
has sub-steps, takes `action` (detect, meetings or documents) too. It returns what the step prints, refusals included
("REFUSED: ..."), and the step's next move.

Run: `uv run python -m ddwb.mcp_server` (stdio). Pinned to the MCP SDK 1.x (`FastMCP`); 2.x renamed the server API.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import os
from typing import Annotated


def unset_placeholders(env=os.environ) -> list[str]:
    """Settings the plugin passes that the user never gave arrive as the literal `${user_config.x}` (Claude Code leaves
    an unknown variable unexpanded), or empty: both mean unset, so the defaults apply. Runs before the paths are read."""
    gone = [k for k, v in env.items() if k.startswith(("DDWB_", "AI_GATEWAY_")) and (not v.strip() or v.strip().startswith("${"))]
    for k in gone:
        del env[k]
    return gone


unset_placeholders()

from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import Field  # noqa: E402

from .deep import tools  # noqa: E402

SERVER = "public-records"


SESSION_HELP = ("the session, <run>:<arm>:<body>: a run label, an arm label and a label for the body, in lower case with "
                "hyphens, e.g. 20261106-ask:claude:jefferson-county-al; the same for every call about one body")


def _sub(p: argparse.ArgumentParser):
    return next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)


def subparsers() -> dict[str, argparse.ArgumentParser]:
    return dict(_sub(tools.parser()).choices)


def steps() -> dict[str, str]:
    """The nav steps and their help text, from the command line's own parser."""
    sub = _sub(tools.parser())
    helps = {c.dest: c.help or "" for c in sub._choices_actions}
    return {name: helps.get(name) or (sp.description or "") for name, sp in sub.choices.items()}


def _actions(p: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in p._actions if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))]


def _param(a: argparse.Action, required: bool) -> inspect.Parameter:
    """One tool parameter from one command-line argument: its type, its help, required or not."""
    if isinstance(a, argparse._StoreTrueAction):
        kind, default = bool, False
    elif a.nargs in ("*", "+"):
        kind, default = list[str] | None, None
    else:
        kind, default = (a.type if a.type in (int, float) else str) | None, None
    text = (a.help or "").replace("%(default)s", str(a.default))
    if a.choices:
        text = (text + "; " if text else "") + "one of: " + ", ".join(map(str, a.choices))
    if a.nargs == "?" and a.option_strings and a.const == "":
        text += " (an empty string: the option alone)"
    annotation = Annotated[kind, Field(description=text or a.dest.replace("_", " "))]
    if required:
        return inspect.Parameter(a.dest, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)
    return inspect.Parameter(a.dest, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)


def _required(a: argparse.Action) -> bool:
    return (not a.option_strings and a.nargs not in ("?", "*")) or bool(a.required)


def argv(p: argparse.ArgumentParser, values: dict) -> list[str]:
    """The command line a step's named values stand for: positionals first, then options."""
    pos, opt = [], []
    for a in _actions(p):
        v = values.get(a.dest)
        if not a.option_strings:
            if v is not None:
                pos += [str(x) for x in v] if isinstance(v, list) else [str(v)]
            continue
        flag = max(a.option_strings, key=len)
        if isinstance(a, argparse._StoreTrueAction):
            opt += [flag] if v else []
        elif v is None:
            continue
        elif a.nargs == "?" and v == "":
            opt.append(flag)
        else:
            opt += [flag, *map(str, v)] if isinstance(v, list) else [flag, str(v)]
    return pos + opt


def call(step: str, session: str, args: list[str] | None = None) -> str:
    """Run one nav step in this process and return everything it printed, a refusal included."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            tools.main([session, step, *(args or [])])
        except SystemExit as e:  # argparse errors and explicit exits come back as text, never kill the server
            if e.code not in (0, None):
                print(f"(exit {e.code})")
    return out.getvalue().strip() or "(no output)"


def make_tool(step: str, p: argparse.ArgumentParser):
    """A tool whose parameters are the step's own (a step with sub-steps, `portal`, adds `action`)."""
    subs = dict(_sub(p).choices) if _sub(p) else None
    params = [inspect.Parameter("session", inspect.Parameter.KEYWORD_ONLY, annotation=Annotated[str, Field(description=SESSION_HELP)])]
    if subs:
        params.append(inspect.Parameter("action", inspect.Parameter.KEYWORD_ONLY, annotation=Annotated[str, Field(
            description="one of: " + "; ".join(f"{n} ({(a.help or '')})" for n, a in
                                                ((c.dest, c) for c in _sub(p)._choices_actions)))]))
        merged: dict[str, argparse.Action] = {}
        for sp in subs.values():
            for a in _actions(sp):
                merged.setdefault(a.dest, a)
        params += [_param(a, False) for a in merged.values()]  # which are needed depends on the action
    else:
        acts = _actions(p)
        params += [_param(a, True) for a in acts if _required(a)] + [_param(a, False) for a in acts if not _required(a)]

    def tool(**values) -> str:
        session = values.pop("session")
        if subs:
            action = values.pop("action")
            if action not in subs:
                return f"REFUSED: action must be one of {', '.join(subs)}"
            return call(step, session, [action, *argv(subs[action], values)])
        return call(step, session, argv(p, values))

    tool.__signature__ = inspect.Signature(params, return_annotation=str)
    tool.__name__ = f"nav_{step}"
    return tool


def build() -> FastMCP:
    server = FastMCP(SERVER)
    helps = steps()
    for name, sp in subparsers().items():
        server.add_tool(make_tool(name, sp), name=name, description=f"nav {name}: {helps[name]}".strip())
    return server


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
