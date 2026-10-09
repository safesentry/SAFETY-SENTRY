# Data and third-party notices

## Dataset summary

The dataset contains step-level snapshots for routing proposed agent tool calls
into `direct_execute`, `ask_human`, or `refuse`. A snapshot includes the user
task, optional persona memory, service, prior tool trajectory, and one proposed
tool call.

The in-domain corpus contains 9,203 records split into 7,767 training and 1,436
test records. A separate 198-record Mailu set evaluates a service held out from
the in-domain corpus.

## Sources

Internal records were constructed against self-hosted instances of Gitea,
Rocket.Chat, ownCloud, NocoDB, Zammad, ERPNext, OpenEMR, and Vaultwarden. Mailu
is the held-out service. The combined corpus also contains adapted records from
When2Call, AT-Bench, AgentHarm, TS-Bench, and R-Judge.

## Construction

The pipeline first runs a task-completion agent against a local service and
captures real tool calls and observations. Two LLM annotators label each
proposed step as a three-way routing record, optionally conditioned on a
persona-memory sidecar; authors arbitrate disagreements. The code under
`safety_pipeline/synthesis/` provides the reference collection and review
runners used around this annotation workflow.

## Intended use

The data is intended for research on tool-call routing, human intervention,
agent safety, and context-dependent authorization. It is not a production
authorization policy and should not be used as the sole control for a live
service.

## Sensitive-looking values

The sandbox tasks intentionally contain synthetic credentials, webhook URLs,
email addresses, and API-like strings so that guards encounter realistic risk
patterns. They must be treated as inert test fixtures and must never be reused
as real credentials.

## Licensing

The root MIT License applies to repository source code. Adapted benchmark
material and containerized services remain subject to their respective
upstream terms. Users are responsible for checking those terms before
redistributing derived subsets.

This repository orchestrates independent open-source services, including
Gitea, Rocket.Chat, ownCloud/oCIS, NocoDB, Zammad, ERPNext, OpenEMR,
Vaultwarden, and Mailu. Their container images and application code are not
relicensed by this repository and remain subject to their upstream licenses.

Some service seeding assets were developed from TheAgentCompany resources. The
retained license notice is available at:

```text
docker/thirdparty/TheAgentCompany_LICENSE
```

The released dataset also contains adapted examples from When2Call, AT-Bench,
AgentHarm, TS-Bench, and R-Judge. Those source projects retain their respective
rights and terms.
