"""Capabilities module for domain-specific intelligence and policies."""
from .base import ActionRiskLevel, BaseWebsiteCapability
from .generic import GenericWebsiteCapability
from .irctc import IrctcCapability
from .leetcode import LeetCodeCapability
from .registry import (
    get_capability_for_domain,
    get_capability_for_service,
    resolve_capability,
)

__all__ = [
    "ActionRiskLevel",
    "BaseWebsiteCapability",
    "GenericWebsiteCapability",
    "IrctcCapability",
    "LeetCodeCapability",
    "get_capability_for_domain",
    "get_capability_for_service",
    "resolve_capability",
]
