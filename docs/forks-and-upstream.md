# Downstream instances and upstream

A downstream repository keeps the shared project's ancestry and its own accepted
changes. `origin` is that repository; `upstream` is the shared source. Instance
configuration, procedures and local patches live downstream. Personal worlds,
credentials and native task records belong in their own private storage.

An independently created private repository can use exactly this arrangement.
GitHub's fork-network relationship is not required for Git fetching and merging.
Start it from a public commit and add only the instance's intended files. Keeping
the current tree while making a new root loses the common ancestor; merging an
old private branch imports its entire reachable history.

## Observe and integrate

Declare the accepted upstream source in the controller's repository configuration:

```yaml
repositories:
  app:
    path: /srv/app
    remote_url: https://github.com/example-private/app.git
    upstream:
      remote_url: https://github.com/example/app.git
      branch: main
```

The controller fetches both sources with its existing Git credentials. It supplies
ordinary `origin/main` and `upstream/main` tracking refs to the agent clone and
keeps their remote URLs true. Fetching upstream leaves the instance's HEAD alone.
An agent's edit to `.git/config` cannot change the controller's accepted source.

An integration task reviews `origin/main..upstream/main`, merges the selected
upstream commit into its work branch, reconciles conflicts and runs the repository's
checks. Cognition decides which changes fit and whether a local patch has become
redundant. The harness publishes the tested result with the instance's current
main as its first parent and the integrated upstream ancestry as additional parents.
It retains only ancestors proved against the configured source, even when upstream
advances while the task is running. Task exploration remains on private task refs.
The next merge therefore sees the actual previous integration as its merge base.

The published commit's `Steward-Work` trailer still names the exact authorized work.
A moved destination causes the usual revalidation; gates test the exact merge commit
that publication attempts. No reset or force-rewrite of the instance's accepted
history is needed to stay current.

## Notification and cognition

[Repository notifications](repository-notifications.md) refresh Git observations.
They do not authorize a merge. An organisation rhythm can watch just the upstream
source through its existing activity paths:

```yaml
rhythms:
  upstream:
    schedule: {quiet: 60}
    procedure: assess-upstream
    input: repositories/app/main
    workdir: /srv
    paths: [repositories/app/upstream/main]
    owner: telegram:3
```

The example assumes an existing read-only `assess-upstream` procedure and a configured
owner with that topic id. Its instructions compare the observed upstream with the
instance, identify relevant work and return findings to the owner. The owner can
admit an integration task through the normal repository authority. Existing
incomplete work must be considered before admitting another integration. The
rhythm's accepted inputs already record which source revisions it has assessed.

The flow is: push notification, fetch, assess, integrate, gate, publish, observe
deployment. A notification alone says nothing about the deployed revision.

## Contributing back

Develop a shared change on a branch based on public upstream. Apply the relevant
downstream commits or patch there, test it, and open a public pull request. A
private downstream normally cannot open a GitHub cross-network pull request
directly; publish the contribution branch in an authorized public repository.
[GitHub's fork rules](https://docs.github.com/en/pull-requests/reference/forks)
describe the visibility and network relationship.

Review the entire contribution ancestry and diff. A merge of the private instance
branch into public trunk would expose its history, including instance-only files
that were later deleted. After a contribution lands upstream, merge upstream back
into the instance and reconcile any duplicate patch. Configuration that varies per
installation should remain configuration; a shared capability belongs upstream.
