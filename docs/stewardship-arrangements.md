# Ways to use and connect stewards

The same harness can steward one repository, an organisation, or a person's
work across organisations. A conventional organisation does not need a personal
steward above it. A person and their one-person company can overlap much more
closely. Neither arrangement should dictate the other's structure. Below, what
works today and what is only intended are kept visibly apart.

The common rule is [knowledge ownership](steward-hierarchy-and-memory.md#ownership-law):
project truth belongs to the project, company truth to the company, and
personal or cross-organisation truth to the personal world. Access can span
those scopes without merging their ownership.

## The choices that determine an arrangement

| Choice | What it determines |
| --- | --- |
| Knowledge owner | Where a fact is maintained as authoritative: repository, organisation world, or personal world |
| Conversation owner | Where the discussion, native session records and derived memory belong |
| Granted authority | What the steward may read, change, publish or deploy |
| Controller layout | Which configured instance executes and accepts the work |

These choices are related, but not interchangeable. Two worlds do not by
themselves require two personas. One persona does not make all its records
company property. Reading another scope does not grant publication authority.
A separate controller only supplies a meaningful trust boundary when its
identities, files and credentials enforce it.

The current configuration gives an instance one optional `world`, not a map
of worlds selected by conversation. Separate scope ownership is therefore a
design requirement to satisfy, not proof that arbitrary arrangements already
fit today's YAML.

## One repository

A steward can work directly in a repository, with project documentation as
its orientation and no separate organisation world. This suits a scope whose
durable knowledge belongs entirely to one project. Repository tasks, gates
and targets still have their ordinary meanings.

There is no need to invent an organisation layer just to run the harness.
Introduce an organisation world when there is actual knowledge spanning
projects to own.

## An organisation with several repositories

In a conventional arrangement, the organisation steward has
its own world for shared intent, policy and relationships between projects.
Each repository retains its implementation and operational truth. A
conversation about a producer and several consumers belongs to the
organisation; changes to each consumer become repository tasks.

This is the ordinary world-plus-managed-repositories arrangement. It needs
no personal steward and no hierarchy of independently running controllers for every
repository. Repository tasks provide the narrower execution context within
the same harness. An organisation world is not necessarily public: its
publication rules must cover conversations and native records as well as
curated company documents.

## Several independent organisations

Independent organisations can each have their own steward, knowledge and
authority. They can run on different hosts or share a provisioned host;
sharing a host does not merge their worlds or credentials.

These instances can operate independently today. A person can carry an
explicit brief or a result reference between them. Automatic cooperation
between stewards is a separate capability: co-location and use of the same
kernel do not establish a delegation channel.

## A personal steward across organisations

A personal world owns personal intent, private context and decisions spanning
organisations. Organisation worlds continue to own their company knowledge. With
granted access, a personal steward can use that knowledge to reason across the
person's work without maintaining competing editable copies of it.

The intended interconnection is a scoped request to an organisation and
attributable evidence back to the personal steward. The organisation retains
control of its repository and deployment authority. The [hierarchy contract](steward-hierarchy-and-memory.md#kernel-boundary)
marks cross-scope delegation as architectural intent. There is no parent/child
transport to switch on in configuration. (There once was a shared-SQLite bus. It
did not enforce the authority boundaries it implied, so it went.)

One personal world can also live on two machines, with private Git replication
between them. That replication belongs to whoever built it. Replicating one
world's state is different from exchanging work between independently owned
organisation worlds, and the harness ships no world replication of its own.

## A person and their own company

Some people want their steward and their company closely intertwined: the
personal steward also stewards the company. That is a legitimate use of the
harness, not a template every organisation must follow.

The required distinction is between private knowledge and company knowledge.
The company needs its own world just as any organisation does. The personal
steward may access it; separating ownership must not remove the broad view that
makes the personal steward useful, and company knowledge should remain usable
without inheriting the operator's personal history.

This does **not** settle the number of controllers or Telegram identities. One
controller can keep the conversation world and register the company's knowledge
repository as a managed repository. Company updates then use scoped repository
tasks: two knowledge homes under one controller, without multi-world
conversation routing.

## Where a Telegram conversation belongs

Discussing the company with a personal steward can remain a personal-world
conversation. The company can receive the resulting brief or company fact
without receiving the surrounding personal discussion. A conversation explicitly
owned by the company instead belongs to the organisation's records.

This matters beyond prompting the model to file its notes correctly.
[Native records](native-provider-runtime.md#native-workflows-and-storage-boundary)
contain conversation and tool data; writable world turns retain them with
their candidate and accepted world history. Reading private context during
a company-owned session can put that context into company-owned records.
Moving a summary afterward does not remove it from those records or history.

Today, Telegram topics distinguish conversations within an instance, but
do not select different worlds. A topic named after a company is not a storage or
authority boundary. Possible future arrangements include separately bound
chats or explicitly routed topics; neither is selected here. Whatever the
interface, the conversation's owner should be visible and established before
recording, and should not silently change when its subject changes.

For an intertwined setup, keeping mixed discussions in the private scope and
passing company-scoped outputs to the company is a coherent option; whether to
add a dedicated company conversation surface is the operator's call. Either way,
the harness must not depend on a promise never to mention personal matters on
Telegram.

## Connections and their current limits

| Connection | Current position |
| --- | --- |
| Organisation conversation to repository task and result | Implemented by the ordinary task and acceptance paths |
| Several separately configured harness instances | Established deployment arrangement; each retains its own state and authority |
| Reading another scope's knowledge | Requires provisioned access; filesystem visibility does not grant writes or publication |
| Replicating one world between machines | Not a harness feature; the instance that wants it owns it |
| Authenticated requests and results between stewards | Architectural intent; no generic delegation transport today |
| Telegram routing to different worlds in one instance | Not implemented by topic naming or the current single-world configuration |

Choose the smallest arrangement that reflects real ownership and authority.
Use the [getting-started guide](getting-started.md) for an actual installation
and the [kernel contract](kernel-contract.md) for implemented boundaries.
This guide does not introduce multi-world routing, a federation framework,
or a requirement to split every role into a separate service.
