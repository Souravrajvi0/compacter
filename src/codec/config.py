"""Project config: <project>/codec.yaml (all sections optional)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core.autocontrol import AutoCompactConfig
from .core.models import Rule
from .core.scopes import ScopeRule, scope_matches, specificity  # noqa: F401  (re-export)

CONFIG_FILE = "codec.yaml"


@dataclass
class AutoTagConfig:
    enabled: bool = True
    min_confidence: float = 0.7
    set_keep_verbatim_for_tags: list[str] = field(default_factory=list)


@dataclass
class Config:
    scopes: list[ScopeRule] = field(default_factory=list)
    auto_tagging: AutoTagConfig = field(default_factory=AutoTagConfig)
    auto_compaction: AutoCompactConfig = field(default_factory=AutoCompactConfig)


def load_config(project: Path) -> Config:
    path = project / CONFIG_FILE
    if not path.exists():
        return Config()
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    scopes = []
    for s in raw.get("scopes", []) or []:
        scopes.append(ScopeRule(match=s["match"], rule=Rule.from_dict(s.get("rule")) or Rule()))
    at_raw = raw.get("auto_tagging", {}) or {}
    auto = AutoTagConfig(
        enabled=at_raw.get("enabled", True),
        min_confidence=float(at_raw.get("min_confidence", 0.7)),
        set_keep_verbatim_for_tags=list(at_raw.get("set_keep_verbatim_for_tags", []) or []),
    )
    ac_raw = raw.get("auto_compaction", {}) or {}
    ac_fields = {k: v for k, v in ac_raw.items() if k in AutoCompactConfig.__dataclass_fields__}
    auto_c = AutoCompactConfig(**ac_fields)
    auto_c.validate()
    return Config(scopes=scopes, auto_tagging=auto, auto_compaction=auto_c)
