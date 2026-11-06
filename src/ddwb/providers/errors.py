"""What a provider call can raise. Every failure is an error with a reason, never an empty answer: a caller that gets
no exception got a real result (an empty search is a result; a failed search is an error)."""

from __future__ import annotations

from typing import Any


class ProviderError(Exception):
    """A call that gave no answer. `fallback`: whether the next provider in a configured chain may be tried (never
    for a pinned call). `result`: the paid answer, when there is one (price guard, bot wall)."""

    fallback = False

    def __init__(self, message: str, *, provider: str | None = None, result: Any = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.result = result


class ConfigError(ProviderError):
    """The settings break a rule (an unknown provider, a key written out instead of ${ENV_NAME}, ...)."""


class MissingKey(ProviderError):
    """A ${ENV_NAME} reference with nothing in the environment. Nothing was sent."""

    fallback = True


class Unavailable(ProviderError):
    """The provider did not answer: transport failure, server error after retries, refused (auth, credits, rate
    limit, trial used up), or the server has no tool for this kind of work."""

    fallback = True


class BadRequest(ProviderError):
    """The provider rejected the request itself (400, 404, 422): fix the call, do not retry elsewhere."""


class LimitReached(ProviderError):
    """This run has used its calls for this provider (settings: max_calls)."""

    fallback = True


class BudgetExceeded(ProviderError):
    """This call's estimate would take the run over its dollar limit (settings: max_usd)."""

    fallback = True


class RunStopped(ProviderError):
    """The price guard stopped this run's paid calls earlier."""


class PriceDrift(ProviderError):
    """The provider's reported charge is far above usage x the price table: the price changed. The call was paid, so
    its answer is in `result`; the run's paid calls stop."""


class BotWall(ProviderError):
    """scrape: the host is already stopped for a bot wall, or the provider's page is a bot wall (dropped, the host
    stopped). Never retried elsewhere and never worked around."""


class UseAgent(ProviderError):
    """search: no search provider here; the agent should use its own web search tool."""


class Off(ProviderError):
    """This kind of work is switched off in the settings (scrape off = plain fetch only; judge off = no Jev)."""


class NotAvailable(ProviderError):
    """Declared but not built yet (browse)."""
