"""Provider settings: which provider does each kind of work, request headers, keys (as references only) and per-run
limits. Each setting is taken from the first place that has it:

    1. environment variables: DDWB_SEARCH, DDWB_SCRAPE, DDWB_JUDGE, DDWB_BROWSE, DDWB_REASON (one spec, or several
       separated by commas: tried in order when one fails), DDWB_<KIND>_HEADERS (a JSON object), DDWB_MAX_CALLS,
       DDWB_MAX_USD
    2. ./providers.toml (the working directory)
    3. ~/.config/public-records/providers.toml
    4. the defaults below

A provider spec is one of:
    mcp:<url>       an MCP server over streamable HTTP; we are the client (keyless servers work as they are)
    api:parallel    Parallel's Search API              (key: PARALLEL_API_KEY)
    api:exa         Exa's search API                    (key: EXA_API_KEY)
    api:firecrawl   Firecrawl's scrape API              (key: FIRECRAWL_API_KEY)
    api:treg        Firecrawl's scrape through treg     (key: TREG_TOKEN)
    api:vercel      Vercel AI Gateway: Jev (judge) and chat models (reason)   (key: AI_GATEWAY_API_KEY)
    agent           search only: the agent uses its own web search tool
    off             not done here (scrape off = our own plain fetch only; judge off = no Jev)

Keys are never stored: settings name them as ${ENV_NAME} references, read from the environment at call time. A key
written out in a settings file is refused. An MCP URL or header may carry a reference too, e.g.
`mcp:https://mcp.exa.ai/mcp?exaApiKey=${EXA_API_KEY}` or `Authorization = "Bearer ${PARALLEL_API_KEY}"`.
See providers.example.toml at the repo root.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ConfigError, MissingKey

KINDS = ("search", "scrape", "judge", "browse", "reason")
VENDORS = ("parallel", "exa", "firecrawl", "treg", "vercel")
KEY_ENV = {"parallel": "PARALLEL_API_KEY", "exa": "EXA_API_KEY", "firecrawl": "FIRECRAWL_API_KEY", "treg": "TREG_TOKEN",
           "vercel": "AI_GATEWAY_API_KEY"}
DEFAULT_CHAINS = {
    "search": ("mcp:https://search.parallel.ai/mcp", "agent"),  # Parallel's keyless search MCP, then the agent's own
    "scrape": ("mcp:https://mcp.firecrawl.dev/v2/mcp", "off"),  # Firecrawl's keyless MCP (trial limits), else fetch only
    "judge": ("off",),  # Jev is optional: the user connects it (api:vercel)
    "browse": ("off",),  # an MCP browser, not used by the tools yet: the browser tier is set by DDWB_BROWSER
    "reason": ("api:vercel",),  # the benchmark harness only
}
ALLOWED = {  # which spec families and vendors each kind accepts
    "search": {"mcp", "api:parallel", "api:exa", "agent", "off"},
    "scrape": {"mcp", "api:firecrawl", "api:treg", "off"},
    "judge": {"api:vercel", "off"},
    "browse": {"mcp", "off"},
    "reason": {"api:vercel", "off"},
}
DEFAULT_JUDGE_MODEL = "typesafe-ai/jev-1.14.0"
DEFAULT_MAX_CALLS = 1000  # per provider, per run
DEFAULT_MAX_USD = 1.00  # estimated total, per run
LOCAL_FILE = "providers.toml"
USER_FILE = Path("~/.config/public-records/providers.toml")

ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
KEY_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
SECRET_HEADER = re.compile(r"auth|key|token|secret|password|cookie", re.I)
SECTIONS = {*KINDS, "limits", "keys", "prices"}
KIND_KEYS = {"use", "headers", "model"}


@dataclass(frozen=True)
class Spec:
    """One provider, as written. `url` keeps any ${ENV_NAME} reference unexpanded: specs are what the ledger and the
    cache see."""

    text: str
    family: str  # mcp | api | agent | off
    vendor: str | None = None  # parallel | exa | firecrawl | vercel; None for an MCP server we do not know
    url: str | None = None

    @property
    def keyed(self) -> bool:
        """Whether the call runs on someone's key (and so is billed): every api: spec; an MCP server only when its
        URL or headers carry a key reference (set by the caller from the settings)."""
        return self.family == "api"

    def __str__(self) -> str:
        return self.text


def vendor_of_url(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for vendor, domain in (("parallel", "parallel.ai"), ("exa", "exa.ai"), ("firecrawl", "firecrawl.dev")):
        if host == domain or host.endswith("." + domain):
            return vendor
    return None


def parse_spec(text: str) -> Spec:
    t = (text or "").strip()
    if t in ("agent", "off"):
        return Spec(t, t)
    if t.startswith("mcp:"):
        url = t[4:].strip()
        parts = urlsplit(url)
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ConfigError(f"provider {t!r}: an MCP server must be an https:// URL (http only for localhost)")
        if not parts.hostname:
            raise ConfigError(f"provider {t!r}: the MCP URL has no host")
        return Spec(t, "mcp", vendor_of_url(url), url)
    if t.startswith("api:"):
        vendor = t[4:].strip()
        if vendor not in VENDORS:
            raise ConfigError(f"provider {t!r}: api: takes one of {', '.join(VENDORS)}")
        return Spec(t, "api", vendor)
    raise ConfigError(f"provider {t!r}: expected mcp:<url>, api:<vendor>, agent or off")


def check_chain(kind: str, specs: tuple[Spec, ...]) -> tuple[Spec, ...]:
    if not specs:
        raise ConfigError(f"{kind}: no provider given")
    for i, s in enumerate(specs):
        family = s.family if s.family != "api" else f"api:{s.vendor}"
        if family not in ALLOWED[kind]:
            raise ConfigError(f"{kind} cannot use {s.text!r}; it takes {', '.join(sorted(ALLOWED[kind]))}")
        if s.family in ("agent", "off") and i != len(specs) - 1:
            raise ConfigError(f"{kind}: {s.text!r} ends a chain; nothing after it would ever be tried")
    return specs


def parse_chain(kind: str, value: Any) -> tuple[Spec, ...]:
    if isinstance(value, str):
        items = [v for v in value.split(",") if v.strip()]
    elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        items = list(value)
    else:
        raise ConfigError(f"{kind}.use must be a string or a list of strings")
    return check_chain(kind, tuple(parse_spec(v) for v in items))


def check_headers(where: str, headers: Any) -> dict[str, str]:
    if not isinstance(headers, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ConfigError(f"{where}: headers must be a table of strings")
    for name, value in headers.items():
        if SECRET_HEADER.search(name) and not ENV_REF.search(value):
            raise ConfigError(f"{where}: header {name!r} looks like a credential; write it as a ${{ENV_NAME}} reference "
                              "(e.g. \"Bearer ${PARALLEL_API_KEY}\"), never the key itself")
    return dict(headers)


def parse_headers_env(name: str, text: str) -> dict[str, str]:
    """DDWB_<KIND>_HEADERS: a JSON object, or `Name: value; Name2: value2`."""
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            return check_headers(name, json.loads(text))
        except ValueError:
            raise ConfigError(f"{name} is not valid JSON") from None
    out = {}
    for part in text.split(";"):
        if part.strip():
            k, sep, v = part.partition(":")
            if not sep:
                raise ConfigError(f"{name}: expected a JSON object or 'Name: value; Name2: value2'")
            out[k.strip()] = v.strip()
    return check_headers(name, out)


def check_key_ref(vendor: str, value: Any) -> str:
    if not isinstance(value, str) or not KEY_REF.match(value.strip()):
        raise ConfigError(f"keys.{vendor} must be a ${{ENV_NAME}} reference such as \"${{{KEY_ENV.get(vendor, 'API_KEY')}}}\"; "
                          "keys are never written into settings")
    return value.strip()


def _number(where: str, value: Any, kind: type) -> Any:
    """A non-negative number, or None for "none" (no limit)."""
    if isinstance(value, str) and value.strip().lower() in ("none", "off", "unlimited"):
        return None
    try:
        n = kind(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} must be a number") from None
    if isinstance(value, bool) or n < 0:
        raise ConfigError(f"{where} must be a non-negative number")
    return n


def read_file(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    unknown = set(data) - SECTIONS
    if unknown:
        raise ConfigError(f"{path}: unknown sections {sorted(unknown)}; expected {sorted(SECTIONS)}")
    for kind in KINDS:
        extra = set(data.get(kind) or {}) - KIND_KEYS
        if extra:
            raise ConfigError(f"{path}: [{kind}] has unknown keys {sorted(extra)}; expected {sorted(KIND_KEYS)}")
    return data


@dataclass(frozen=True)
class Settings:
    chains: Mapping[str, tuple[Spec, ...]]
    headers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    keys: Mapping[str, str] = field(default_factory=dict)  # vendor -> ${ENV_NAME}
    max_calls: int | None = DEFAULT_MAX_CALLS
    max_usd: float | None = DEFAULT_MAX_USD
    judge_model: str = DEFAULT_JUDGE_MODEL
    prices: Mapping[str, Any] = field(default_factory=dict)  # overrides of cost.PRICES (models only, for now)
    sources: Mapping[str, str] = field(default_factory=dict)  # setting -> where it came from

    def chain(self, kind: str) -> tuple[Spec, ...]:
        return self.chains.get(kind) or check_chain(kind, tuple(parse_spec(s) for s in DEFAULT_CHAINS[kind]))

    def key_ref(self, vendor: str) -> str:
        return self.keys.get(vendor) or "${" + KEY_ENV[vendor] + "}"

    def describe(self) -> dict:
        """What is in force and from where. Holds references, never a key."""
        return {"chains": {k: [s.text for s in self.chain(k)] for k in KINDS},
                "headers": {k: sorted(v) for k, v in self.headers.items() if v},
                "keys": {v: self.key_ref(v) for v in VENDORS}, "max_calls": self.max_calls, "max_usd": self.max_usd,
                "judge_model": self.judge_model, "sources": dict(self.sources)}


def defaults() -> Settings:
    return Settings({k: check_chain(k, tuple(parse_spec(s) for s in v)) for k, v in DEFAULT_CHAINS.items()},
                    sources={k: "default" for k in KINDS})


def load(env: Mapping[str, str] | None = None, *, local: Path | str | None = None,
         user: Path | str | None = None) -> Settings:
    """Settings from the environment, then ./providers.toml, then the user's file, then the defaults."""
    env = os.environ if env is None else env
    local = Path(local) if local is not None else Path.cwd() / LOCAL_FILE
    user = Path(user) if user is not None else USER_FILE.expanduser()
    layers = [(str(p), read_file(p)) for p in (local, user) if p.is_file()]

    def first(path: tuple[str, ...]) -> tuple[Any, str] | None:
        for where, data in layers:
            node: Any = data
            for part in path:
                node = node.get(part) if isinstance(node, Mapping) else None
            if node is not None:
                return node, where
        return None

    chains, headers, sources = {}, {}, {}
    for kind in KINDS:
        name = f"DDWB_{kind.upper()}"
        if env.get(name, "").strip():
            chains[kind], sources[kind] = parse_chain(kind, env[name]), f"env {name}"
        elif (hit := first((kind, "use"))) is not None:
            chains[kind], sources[kind] = parse_chain(kind, hit[0]), hit[1]
        else:
            chains[kind], sources[kind] = check_chain(kind, tuple(parse_spec(s) for s in DEFAULT_CHAINS[kind])), "default"
        hname = f"DDWB_{kind.upper()}_HEADERS"
        if env.get(hname, "").strip():
            headers[kind], sources[f"{kind}.headers"] = parse_headers_env(hname, env[hname]), f"env {hname}"
        elif (hit := first((kind, "headers"))) is not None:
            headers[kind], sources[f"{kind}.headers"] = check_headers(f"{hit[1]} [{kind}]", hit[0]), hit[1]

    limits = {}
    for key, envname, kind, default in (("max_calls", "DDWB_MAX_CALLS", int, DEFAULT_MAX_CALLS),
                                        ("max_usd", "DDWB_MAX_USD", float, DEFAULT_MAX_USD)):
        if env.get(envname, "").strip():
            limits[key], sources[key] = _number(envname, env[envname], kind), f"env {envname}"
        elif (hit := first(("limits", key))) is not None:
            limits[key], sources[key] = _number(f"{hit[1]} limits.{key}", hit[0], kind), hit[1]
        else:
            limits[key], sources[key] = default, "default"

    keys = {}
    for vendor in VENDORS:
        if (hit := first(("keys", vendor))) is not None:
            keys[vendor] = check_key_ref(vendor, hit[0])
    for where, data in layers:  # a key written out anywhere is refused, even one a higher layer overrides
        for vendor, value in (data.get("keys") or {}).items():
            if vendor not in VENDORS:
                raise ConfigError(f"{where}: keys.{vendor}: unknown vendor; expected {', '.join(VENDORS)}")
            check_key_ref(vendor, value)

    judge_model = DEFAULT_JUDGE_MODEL
    if (hit := first(("judge", "model"))) is not None:
        judge_model = str(hit[0])
    prices: dict = {}
    for _where, data in reversed(layers):  # the nearer file wins model by model
        for model, row in ((data.get("prices") or {}).get("models") or {}).items():
            prices[model] = row
    return Settings(chains, headers, keys, limits["max_calls"], limits["max_usd"], judge_model, {"models": prices}, sources)


def expand(text: str, env: Mapping[str, str] | None = None) -> tuple[str, list[str]]:
    """${ENV_NAME} references replaced from the environment, at call time only. Returns the text and the secret values
    used (so error messages can be scrubbed of them)."""
    env = os.environ if env is None else env
    used: list[str] = []

    def sub(m: re.Match) -> str:
        value = (env.get(m.group(1)) or "").strip()
        if not value:
            raise MissingKey(f"{m.group(1)} is not set in the environment (settings name it as ${{{m.group(1)}}})")
        used.append(value)
        return value

    return ENV_REF.sub(sub, text or ""), used


def has_ref(text: str) -> bool:
    return bool(ENV_REF.search(text or ""))
