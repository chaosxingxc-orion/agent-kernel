"""Verifies for known effect classes and validate effect class."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.action_type_registry import (
    KNOWN_EFFECT_CLASSES,
    ActionTypeDescriptor,
    validate_effect_class,
)


class TestKnownEffectClasses:
    """Test suite for KnownEffectClasses."""

    def test_contains_read_only(self) -> None:
        """Verifies contains read only."""
        assert "read_only" in KNOWN_EFFECT_CLASSES

    def test_contains_compensatable_write(self) -> None:
        """Verifies contains compensatable write."""
        assert "compensatable_write" in KNOWN_EFFECT_CLASSES

    def test_contains_fire_forget(self) -> None:
        """Verifies contains fire forget."""
        assert "fire_forget" in KNOWN_EFFECT_CLASSES

    def test_contains_irreversible_write(self) -> None:
        """Verifies contains irreversible write."""
        assert "irreversible_write" in KNOWN_EFFECT_CLASSES

    def test_is_frozenset(self) -> None:
        """Verifies is frozenset."""
        assert isinstance(KNOWN_EFFECT_CLASSES, frozenset)


class TestValidateEffectClass:
    """Test suite for ValidateEffectClass."""

    def test_known_class_returns_true(self) -> None:
        """Verifies known class returns true."""
        assert validate_effect_class("read_only") is True

    def test_known_class_strict_returns_true(self) -> None:
        """Verifies known class strict returns true."""
        assert validate_effect_class("compensatable_write", strict=True) is True

    def test_unknown_class_returns_false(self) -> None:
        """Verifies unknown class returns false."""
        assert validate_effect_class("invented_class") is False

    def test_unknown_class_strict_raises(self) -> None:
        """Verifies unknown class strict raises."""
        with pytest.raises(ValueError, match="invented_strict"):
            validate_effect_class("invented_strict", strict=True)

    def test_all_known_classes_pass(self) -> None:
        """Verifies all known classes pass."""
        for cls in KNOWN_EFFECT_CLASSES:
            assert validate_effect_class(cls) is True


class TestActionTypeDescriptorAllowedEffectClasses:
    """Test suite for ActionTypeDescriptorAllowedEffectClasses."""

    def test_default_is_empty_frozenset(self) -> None:
        """Verifies default is empty frozenset."""
        d = ActionTypeDescriptor(action_type="x", description="test")
        assert d.allowed_effect_classes == frozenset()

    def test_custom_allowed_effect_classes(self) -> None:
        """Verifies custom allowed effect classes."""
        d = ActionTypeDescriptor(
            action_type="guarded_write",
            description="Only allows writes",
            allowed_effect_classes=frozenset({"compensatable_write", "irreversible_write"}),
        )
        assert "compensatable_write" in d.allowed_effect_classes
        assert "read_only" not in d.allowed_effect_classes
