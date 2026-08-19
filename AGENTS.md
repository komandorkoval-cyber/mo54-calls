# Critical system boundary: MO54 vs MO54 Calls

This repository and the current task concern **MO54 Calls** only.  MO54 Calls
is an independent calls/deals CRM.  **MO54** is a separate production planning
application and is a forbidden zone unless the user gives explicit, direct
permission for a specific action.

## Absolute prohibitions for MO54 work

Do not read, copy, modify, deploy, restart, rebuild, migrate, configure, or
otherwise operate on any MO54 resource.  This includes MO54 code, databases,
schemas, `.env` files, secrets, Docker Compose resources, containers, volumes,
networks, systemd units, reverse proxy configuration, production paths, and
planner data.  Do not create an integration or data dependency between MO54 and
MO54 Calls.  Do not perform SSH/server operations that could affect MO54.

Before any deployment-related action, classify the target as MO54 Calls or
MO54.  If ownership is ambiguous, do not touch it.  If a requested MO54 Calls
feature requires access to or integration with MO54, stop that part and report
the conflict instead of making the change.

## Current P0 scope

P0 changes are permitted only in the isolated `p0-season-terrace` worktree and
only for MO54 Calls code, database migrations, tests, and interface.  This
boundary takes priority over implementation convenience or architectural
assumptions.
