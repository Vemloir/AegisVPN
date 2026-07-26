"""Outbound node control-plane protocol and desired-state publication."""

from .state import build_desired_items, canonical_json, publish_snapshot

__all__ = ["build_desired_items", "canonical_json", "publish_snapshot"]
