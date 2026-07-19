"""Generic config.yml loading: read YAML, validate against a pydantic model.

Every service in this repo (ingest, mcp-server, reranker) has its own
config.yml and its own pydantic schema for it, but the read/validate/report
boilerplate around that was identical three times over -- this is that
boilerplate, factored out once. Each service still owns its own schema
(pydantic model) and just calls load_yaml_config(path, ItsOwnModel).
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from _common.logging_config import get_logger

logger = get_logger(__name__)

ConfigT = TypeVar("ConfigT", bound=BaseModel)


class RawConfig:
    """A config.yml, parsed but not yet validated. Split out from
    load_yaml_config so a service that needs to inject an env-sourced value
    into the raw dict before validation (e.g. mcp-server's AI_GATEWAY_REASONING_MODEL
    overriding search_tools.reasoning.model) has somewhere to do that -- see
    mcp_server/src/server.py."""

    def __init__(self, raw: dict, path: Path) -> None:
        self.raw = raw
        self._path = path

    def validate(self, model_cls: type[ConfigT]) -> ConfigT:
        """Validate the (possibly overridden) raw dict against model_cls.
        Logs a clear error and exits the process on a schema violation --
        there's no reasonable way to run with a broken config, so callers
        just get a valid config back or the process exits."""
        try:
            return model_cls.model_validate(self.raw)
        except ValidationError as exc:
            logger.error("invalid %s:\n%s", self._path, exc)
            raise SystemExit(1) from exc


def load_raw_config(path: Path) -> RawConfig:
    """Read path as YAML without validating it yet. Logs a clear error and
    exits the process if the file is missing."""
    if not path.is_file():
        logger.error("missing config file: %s", path)
        raise SystemExit(1)
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return RawConfig(raw, path)


def load_yaml_config(path: Path, model_cls: type[ConfigT]) -> ConfigT:
    """Read, validate, and return path as model_cls in one step -- for the
    common case where nothing needs to override the raw dict first."""
    return load_raw_config(path).validate(model_cls)
