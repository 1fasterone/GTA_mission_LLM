# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability, exposed credential,
unsafe generated-code path, or dependency compromise.

Use GitHub's private reporting form:

<https://github.com/1fasterone/GTA_mission_LLM/security/advisories/new>

Include the affected commit, reproduction steps, impact, and any proposed
mitigation. Remove live secrets and personal data from the report. You should
receive an acknowledgement within seven days.

## Supported versions

Security fixes are made on the latest `main` branch. Older commits and generated
projects are not independently supported.

## Scope

Useful reports include:

- credential disclosure or secret-handling failures;
- command, template, archive, or path injection;
- unsafe trust-boundary crossings between LLM output and deterministic codegen;
- malicious project or asset inputs that escape their intended directory;
- vulnerable dependencies with a demonstrated impact on this project.

Generated gameplay defects without a security impact belong in the public issue
tracker.
