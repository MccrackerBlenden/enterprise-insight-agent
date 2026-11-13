# Security & responsible demo use

This repository is a **local-only, synthetic-data demonstration**, not a production service. Do not expose `compose.yaml` or `compose.demo.yaml` to the public internet. There is no production authentication, authorization, RBAC, multi-tenancy, rate limiting, or operational SLA. The `EIA_OPS_WRITE_TOKEN` / `EIA_DEMO_WRITE_TOKEN` guard is only for manually authorized **local** state changes.

- Never commit `backend/.env`, API keys, write tokens, database files, `.demo_runs/`, live logs or evaluation outputs.
- Model API credentials are separate from local operations authorization; using a paid provider is optional and user initiated.
- The demo is designed to avoid external shipping, customer notifications or production write operations; only synthetic SQLite state changes are supported.
- `scripts/check_release_safety.py` is heuristic and checks current tracked paths only. `scripts/check_history_paths.py` checks high-risk historical filenames, **not historical secret contents**. A thorough history/secret scan and manual data/license review remain necessary.
- If a real secret is accidentally exposed, revoke/rotate it first. Removing it from the latest commit does **not** necessarily remove it from Git history or existing clones.
- When reporting a suspected vulnerability, avoid pasting credentials, personal data or live system URLs in a public issue.

Do not claim that passing offline tests proves readiness for a public server or real business deployment.
