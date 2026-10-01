"""既存 import の互換入口。実装の正本は adapters/discord/。"""
from importlib import import_module
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
for _name in ("cards", "actions", "delivery", "tasks"):
    _module = import_module(f"adapters.discord.{_name}")
    globals()[_name] = _module
    sys.modules[f"{__name__}.{_name}"] = _module
