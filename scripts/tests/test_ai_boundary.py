"""verify that provider credentials and paid calls remain inside ai-core."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_KEYS = ("OPENAI_API_KEY", "VOYAGE_API_KEY")
SOURCE_SUFFIXES = {".py", ".go", ".ts", ".js", ".toml", ".txt", ".mod", ".json", ".yml", ".yaml"}
SKIPPED_DIRS = {
    "__pycache__",
    "node_modules",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "tiktoken",
}
PROVIDER_CODE = re.compile(
    r"^\s*(?:import|from)\s+(?:openai|voyageai|anthropic)\b"  # a python sdk import
    r"|github\.com/(?:openai|anthropics|sashabaranov)/"  # a go sdk module
    r"|^\s*[\"']?(?:openai|voyageai|anthropic)\s*(?:[=<>~!\[;\"']|$)"  # a python requirement
    r"|api\.(?:openai|voyageai|anthropic)\.com",  # a provider's api host
    re.MULTILINE,
)


def services_given(compose: dict, key: str) -> set[str]:
    """the compose services whose definition mentions `key` anywhere: environment, command or otherwise."""
    return {name for name, service in compose["services"].items() if key in yaml.safe_dump(service)}


def provider_code(roots: list[Path]) -> list[str]:
    """files under `roots` that import a provider sdk, depend on one, or call a provider's api host."""
    found = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in SOURCE_SUFFIXES or SKIPPED_DIRS & set(path.parts) or not path.is_file():
                continue
            if PROVIDER_CODE.search(path.read_text(encoding="utf-8", errors="replace")):
                found.append(path.relative_to(ROOT if root.is_relative_to(ROOT) else root).as_posix())
    return found


def test_only_ai_core_gets_the_provider_keys() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for key in PROVIDER_KEYS:
        assert services_given(compose, key) == {"ai-core"}, key
    # an env_file would hand a service every value in .env, the keys included.
    assert [name for name, s in compose["services"].items() if "env_file" in s] == []


def test_no_other_service_calls_a_model_provider() -> None:
    roots = [p for p in sorted((ROOT / "services").iterdir()) if p.is_dir() and p.name != "ai-core"]
    roots.append(ROOT / "packages")
    assert len(roots) > 5
    assert provider_code(roots) == []


def test_the_scan_finds_provider_code(tmp_path: Path) -> None:
    """the check above would catch each way a service could reach a provider, and ai-core's own code."""
    planted = {
        "a.py": "import openai\n",
        "b.py": "from anthropic import Anthropic\n",
        "requirements.txt": "fastapi==0.1\nvoyageai==0.3.2\n",
        "c.go": 'import "github.com/openai/openai-go"\n',
        "d.ts": "fetch('https://api.openai.com/v1/chat/completions')\n",
        "clean.py": "import openai_client  # a local module, not the SDK\nopenai_model = 'x'\n",
    }
    for name, text in planted.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    # scratch files can live inside the workspace on windows, where the scanner reports repo-relative paths.
    assert [Path(name).name for name in provider_code([tmp_path])] == [
        "a.py",
        "b.py",
        "c.go",
        "d.ts",
        "requirements.txt",
    ]
    assert provider_code([ROOT / "services" / "ai-core"]) != []
