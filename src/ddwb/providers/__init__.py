"""The provider layer: search, scrape, judge (Jev), reason (benchmark only) and browse (not built yet), each done by a
configurable provider: a vendor's MCP server (keyless where the vendor allows), a vendor API on the user's key, the
agent's own tool (search), or off. Settings: ddwb.providers.settings; the cost layer: ddwb.providers.cost."""

from .core import Providers, Result, new_run_id
from .errors import (BadRequest, BotWall, BudgetExceeded, ConfigError, LimitReached, MissingKey, NotAvailable, Off,
                     PriceDrift, ProviderError, RunStopped, Unavailable, UseAgent)
from .settings import Settings, load as load_settings

PARALLEL, EXA, FIRECRAWL, VERCEL = "api:parallel", "api:exa", "api:firecrawl", "api:vercel"
JEV = VERCEL  # Jev runs on the Vercel AI Gateway

__all__ = ["Providers", "Result", "Settings", "load_settings", "new_run_id", "PARALLEL", "EXA", "FIRECRAWL", "VERCEL", "JEV",
           "ProviderError", "ConfigError", "MissingKey", "Unavailable", "BadRequest", "LimitReached", "BudgetExceeded",
           "RunStopped", "PriceDrift", "BotWall", "UseAgent", "Off", "NotAvailable"]
