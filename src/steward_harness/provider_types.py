"""Provider selections shared by configuration and runtime boundaries."""

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

# Provider identity is adapter-owned.  Keeping this as a closed Literal made
# every lifecycle consumer part of the provider registry and meant adding an
# adapter required edits throughout the harness.
ProviderFamily: TypeAlias = str
ProviderProfile = Literal["fast", "balanced", "deep"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelChoice(BaseModel):
    """What a configuration may say about one provider profile's model.

    Effort lives here because it lives with the model everywhere else: the
    built-in table is `(model, effort)` pairs, and a configuration that could
    replace only half of one was a half-configuration. It is optional and its
    absence is a declaration rather than a gap — no effort means the adapter
    sends no effort flag and the provider chooses. That spelling exists for a
    reason: `glm` runs through the Claude adapter against a third-party
    gateway, so it inherits Claude's effort vocabulary without any promise
    that the gateway accepts it.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    effort: ReasoningEffort | None = None
