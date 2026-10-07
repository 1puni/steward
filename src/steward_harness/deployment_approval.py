"""Controller-owned consent for an exact revision at a publication/apply boundary."""

import hashlib
import json
from pathlib import Path
import re
import time

from steward_harness.lease import Lease
from steward_harness.receipts import write_receipt


class DeploymentApprovals:
    def __init__(self, state, operators):
        self.state = state
        self.operators = tuple(operators)
        self.root = Path(str(state.path) + ".deployment-approvals")

    def _identity(self, kind, name, revision, policy):
        if kind not in {"target", "publication"} or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("approval requires target/publication and a full 40-character revision")
        binding = dict(kind=kind, name=name, revision=revision, policy=policy,
                       operators=sorted(self.operators))
        key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        return key, binding

    def require(self, kind, name, revision, policy):
        """Retain the request and notification once; never infer consent from a prompt."""
        key, binding = self._identity(kind, name, revision, policy)
        path = self.root / (key + ".json")
        with Lease(self.state.path.parent, lock_name="deployment-approvals.lock"):
            record = json.loads(path.read_text()) if path.exists() else binding
            if record.get("approved_by") in self.operators:
                return True
            if not path.exists():
                write_receipt(path, record)
            source = "deployment-approval:" + key
            if not self.state.result_receipt(source):
                text = (f"Operator approval required for {kind} {name} at {revision}.\n"
                        f"Review that exact revision, then use /git approve {kind} {name} {revision} "
                        "(use !git in Slack). A changed revision needs separate approval.")
                self.state.save_result_receipt(dict(source_key=source, owner=None, task_id=None,
                    result_text=text, reply=text, done=False))
            return False

    def approve(self, kind, name, revision, policy, actor):
        if actor not in self.operators:
            raise ValueError("only a configured deployment operator can approve deployment")
        key, _ = self._identity(kind, name, revision, policy)
        path = self.root / (key + ".json")
        with Lease(self.state.path.parent, lock_name="deployment-approvals.lock"):
            if not path.exists():
                raise ValueError("no pending approval for this exact revision and current policy")
            record = json.loads(path.read_text())
            if record.get("approved_by") not in self.operators:
                record.update(approved_by=actor, approved_at=time.time())
                write_receipt(path, record)
        return f"Approved {kind} {name} at {revision} by {actor}. The controller will recheck gates before applying it."

    def pending(self):
        return [record for path in sorted(self.root.glob("*.json"))
                if not (record := json.loads(path.read_text())).get("approved_by")]
