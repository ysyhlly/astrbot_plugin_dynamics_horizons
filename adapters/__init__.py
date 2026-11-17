"""External read-only information source adapters for `astrbot_plugin_dynamics_horizons`."""

from __future__ import annotations

from .registry import (
    MASK_TOKEN,
    SOURCE_KINDS,
    SSRFBlockedError,
    build_manual_candidate,
    fetch_item_content,
    is_masked_secret,
    mask_secrets_in_dict,
    merge_secret_config,
    parse_local_ebook,
    poll_source,
    search_public_ebooks,
    validate_source_config,
)

__all__ = [
    "MASK_TOKEN",
    "SOURCE_KINDS",
    "SSRFBlockedError",
    "build_manual_candidate",
    "fetch_item_content",
    "is_masked_secret",
    "mask_secrets_in_dict",
    "merge_secret_config",
    "parse_local_ebook",
    "poll_source",
    "search_public_ebooks",
    "validate_source_config",
]
