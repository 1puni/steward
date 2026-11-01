# Repository notifications

The optional relay accepts signed GitHub push webhooks and sends scoped
repository/ref invalidations over an outbound event stream. Git fetch remains the
source of revision identity. The relay stores no repository contents, Git
credentials or durable event log.

Run `uv run python -m steward_harness.world_events --config /absolute/private.json`
behind HTTPS with proxy buffering disabled. Its private JSON has two mappings:
`repositories`, from repository names to `{ref, secret}`, and `readers`, from bearer
tokens to authorized repository names. Secrets and tokens must each be at least
32 characters. Keep the file and service environment outside tracked repositories.
Configure GitHub's active push hook to POST to the relay's `/github` endpoint using
that repository's secret.

Consumers use `/events`. Every connection starts with a reconciliation notice for
its scope, followed by coalesced invalidations and heartbeat comments. A reconnect
fetches current Git truth, so missed notices require no replay log. Signature,
branch and reader scope checks happen at the relay; consumers match notices only
to their controller-configured repositories, world and upstream sources.

Set `STEWARD_WORLD_EVENTS_URL` and `STEWARD_WORLD_EVENTS_TOKEN` in the controller's
protected environment. HTTPS is required except for HTTP on loopback. Notices
queue the existing credential-controlled fetch and clone refresh; failures retain
the need to retry. A new notice arriving during a refresh remains pending.
Subscribed readers also reconcile hourly. Upstream sources without a confirmed
subscription use a five-minute fallback; publication verifies its source afresh.
Existing rhythm and target observations
retain their own checks; notifications do not replace deployment observation.

A world's local changes remain immediately eligible for publication. Its ordinary
periodic exchange continues until a matching scoped notice confirms subscription;
then an hourly reconciliation backs up the event stream. Neither a configured URL
alone nor an unrelated repository notice suppresses that periodic exchange.

Use the same relay for [upstream integration](forks-and-upstream.md). Grant a
downstream reader the public source's repository/ref scope. Receiving that notice
refreshes `upstream/main`; the configured rhythm and cognition decide what to do.
