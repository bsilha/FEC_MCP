"""Regression coverage for demo/app.py's tool wrappers.

demo/app.py hand-writes a thin @beta_tool wrapper around each real tool
function in fec_mcp.server, so the two can drift out of sync silently: a
wrapper's parameter list is only checked against the real function at call
time (a TypeError on an unexpected/missing keyword), not at import time or
by any type checker, since @beta_tool's decorator hides the underlying
function from static analysis. This happened for real with
get_reporting_calendar (demo/app.py had a stale "calendar_year" parameter
that fec_mcp.server.get_reporting_calendar had never had -- every call
failed with "unexpected keyword argument 'calendar_year'"). This test
inspects every wrapper's real (undecorated) signature against its
corresponding server function's signature so a mismatch fails fast in CI
instead of only surfacing when a user asks the demo the wrong question.
"""

import importlib.util
import inspect
import sys
from pathlib import Path

from fec_mcp import server

DEMO_APP_PATH = Path(__file__).resolve().parents[1] / "demo" / "app.py"


def _load_demo_app():
    spec = importlib.util.spec_from_file_location("fec_mcp_demo_app", DEMO_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demo_app = _load_demo_app()


def test_tool_wrapper_signatures_match_server_functions():
    mismatches = []
    for tool in demo_app.TOOLS:
        server_func = getattr(server, tool.name, None)
        assert server_func is not None, f"server.py has no matching function for tool {tool.name!r}"

        wrapper_params = inspect.signature(tool.func).parameters
        server_params = inspect.signature(server_func).parameters

        if wrapper_params.keys() != server_params.keys():
            mismatches.append(
                f"{tool.name}: wrapper params {list(wrapper_params)} != "
                f"server params {list(server_params)}"
            )

    assert mismatches == [], "\n".join(mismatches)
