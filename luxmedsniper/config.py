"""Reading the YAML configuration and turning it into the objects the rest of the app wants.

None of this has anything to do with the portal, which is why it no longer hangs off the client
class as a pair of static methods.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import ValidationError

from .models import DoctorLocator, LocatorSet, LuxmedSniperError

SECRET_KEYS = frozenset({"password", "api_token", "user_key", "access_token", "token"})


def redacted(config: dict[str, Any]) -> dict[str, Any]:
    """Copy of the config with secrets masked, so LOGURU_LEVEL=DEBUG stays safe to turn on."""
    return {
        key: (
            redacted(value)
            if isinstance(value, dict)
            else "***" if key in SECRET_KEYS and value else value
        )
        for key, value in config.items()
    }


def load_configuration(configuration_files: Iterable[str]) -> dict[str, Any]:
    def merge(a: dict[str, Any], b: dict[str, Any], error_path: str = "") -> dict[str, Any]:
        for kb, vb in b.items():
            va = a.get(kb, vb)
            if va == vb:
                a[kb] = va
            elif isinstance(a[kb], dict) and isinstance(vb, dict):
                merge(a[kb], vb, f"{error_path}.{kb}")
            else:
                err_msg = f"Conflict at {error_path}.{kb}"
                raise LuxmedSniperError(err_msg)
        return a

    config: dict[str, Any] = {}
    for configuration_file in configuration_files:
        configuration_path = Path(configuration_file).expanduser()
        with configuration_path.open(encoding="utf-8") as stream:
            cf = yaml.safe_load(stream)
            config = merge(config, cf)

    logger.debug("Configuration: {}", redacted(config))

    return config


def build_locators(config: dict[str, Any]) -> LocatorSet:
    """Every configured search, parsed one at a time.

    The context is how `luxmedsniper.facilities_ids` reaches the locator: the validator appends
    those ids to the ones spelled out in the locator's own `id`.

    One unparseable locator must not cost the others their searches, so each is validated on its
    own and the failures are handed back for the caller to count. Validating the whole list up
    front would mean a single typo silently stops every working search instead.
    """
    luxmedsniper_config = config["luxmedsniper"]
    context = {"luxmedsniper_config": luxmedsniper_config}

    locators: list[DoctorLocator] = []
    errors: list[str] = []
    for raw in luxmedsniper_config["doctor_locators"]:
        try:
            locators.append(DoctorLocator.model_validate(raw, context=context))
        except ValidationError as e:
            raw_id = raw.get("id") if isinstance(raw, dict) else raw
            # The message is the pydantic one, which quotes only the locator - no credentials.
            errors.append(f"locator {raw_id!r}: {e.errors()[0]['msg']}")
            logger.error("Skipping unparseable locator {!r}", raw_id)

    return LocatorSet(locators=locators, errors=errors)
