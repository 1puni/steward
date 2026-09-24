"""Settings for the installed systemd-release driver, outside kernel config."""
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from steward_harness.config.schema import CommandSpec

class SystemdReleaseConfig(BaseModel):
    """Configured systemd-release deployment policy for a repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_build: CommandSpec | None = None
    service: str | None = None
    release_root: str = "/opt/steward-releases"
    current_symlink: str = "/opt/steward-current"
    health_url: str | None = None
    timeout_seconds: int = Field(default=90, ge=10, le=600)

    @model_validator(mode="after")
    def validates_paths(self) -> "SystemdReleaseConfig":
        if not Path(self.release_root).is_absolute():
            raise ValueError("systemd release_root must be absolute")
        if not Path(self.current_symlink).is_absolute():
            raise ValueError("systemd current_symlink must be absolute")
        if Path(self.release_root) == Path("/"):
            raise ValueError("systemd release_root must not be the filesystem root")
        if Path(self.current_symlink) == Path("/"):
            raise ValueError("systemd current_symlink must not be the filesystem root")
        return self
