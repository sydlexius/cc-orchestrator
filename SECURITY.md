# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| latest  | Yes       |

Only the latest commit on main receives security updates.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Use [GitHub Security Advisories](https://github.com/sydlexius/cc-orchestrator/security/advisories/new)
to report vulnerabilities privately. This ensures the issue can be triaged and a
fix prepared before public disclosure.

When reporting, please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The affected version(s) or commit(s)
- A suggested fix, if you have one

You should receive an initial acknowledgment within 72 hours. Critical issues
will be addressed as quickly as practical.

## Scope

In scope:

- The orchestration scripts and hooks under `scripts/` and `.claude/`
- The deterministic guard (`orchestrate-guard.sh`) and its bypass paths
- Any handling of GitHub tokens or credentials by the tooling

Out of scope:

- Vulnerabilities in upstream dependencies (report those upstream)
- Issues requiring local shell access to a machine already running the tooling
- Behavior of Claude Code itself (report to Anthropic)

## Security Measures

- **Pinned actions:** all GitHub Actions are pinned to a commit SHA
- **Least-privilege tokens:** workflow permissions are declared at job level
- **No persisted credentials:** checkouts use `persist-credentials: false`
- **CodeQL:** Python and workflow (`actions`) analysis run on every PR
- **Secret scanning** with push protection is enabled
