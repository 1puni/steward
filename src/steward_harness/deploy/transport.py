"""The systemd driver's target-scoped release provenance."""
from steward_harness.git import validate_object_id
from steward_harness.git_transport import GitTransportError


class ReleaseTransport:
    def __init__(self, source, target, branch):
        self.source, self.target, self.branch = source, target, branch

    def __getattr__(self, name):
        return getattr(self.source, name)

    def resolve_remote_commit(self, sha):
        validate_object_id(sha)
        self.source.fetch()
        tip = self.source._run("rev-parse", "--verify", f"refs/steward/remote/{self.branch}")
        if not self.source._contains(sha, tip):
            raise GitTransportError("release revision is not on the configured target ref")
        return sha

    def deployed_sha(self):
        return self.source._run("for-each-ref", "--format=%(objectname)",
                                f"refs/steward/targets/{self.target}/healthy") or None

    def set_deployed(self, sha):
        validate_object_id(sha)
        self.source._run("update-ref", f"refs/steward/targets/{self.target}/healthy", sha)
