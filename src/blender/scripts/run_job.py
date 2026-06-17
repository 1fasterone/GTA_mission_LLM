"""
DEPRECATED — superseded by backend parity (Phase 1).

This standalone primitive sanitizer used to be what `blender --background` ran.
It diverged from the live MCP backend (it built bare primitives, not the recipe
library), so the same job produced different assets depending on backend.

Both backends now run the SAME recipe-emitted script produced by
`src.blender.mcp_backend.build_job_code(job, out_dir, write_manifest=True)` —
geometry recipes, palette materials, rig/anim/LOD, softbody→Alembic, timestamp
normalization — so headless and live output are identical.

Kept only so historical references resolve; not invoked by the manager anymore.
"""

import sys

if __name__ == "__main__":
    sys.stderr.write(
        "run_job.py is deprecated and no longer used. The Blender Job Manager "
        "now emits the recipe script via mcp_backend.build_job_code() for both "
        "the subprocess and live-MCP backends.\n"
    )
    sys.exit(0)
