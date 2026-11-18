"""keep go and python latency histograms comparable: the collector drops a histogram with other bounds."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def go_bounds() -> list[int]:
    source = (ROOT / "packages/platform/http_metrics.go").read_text(encoding="utf-8")
    found = re.search(r"httpHeaderBoundsMS = \[\.\.\.\]int\{([^}]*)\}", source)
    assert found, "http_metrics.go no longer declares httpHeaderBoundsMS as one array literal"
    return [int(value) for value in found[1].split(",")]


def python_bounds() -> list[int]:
    tree = ast.parse((ROOT / "packages/python-platform/learning_transport/metrics.py").read_text("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_header_bounds_ms" for target in node.targets
        ):
            return list(ast.literal_eval(node.value))
    raise AssertionError("metrics.py no longer declares _header_bounds_ms")


def test_go_and_python_services_bucket_header_latency_alike() -> None:
    bounds = go_bounds()
    assert bounds == sorted(set(bounds))
    assert python_bounds() == bounds
