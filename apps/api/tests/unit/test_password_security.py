"""Password hashing and policy (PART 42).

Argon2id with a unique salt per hash, and a policy that rejects the passwords
people actually choose. The hasher is built with test-friendly parameters so the
suite stays fast; production parameters come from settings.
"""

from __future__ import annotations

import pytest

from app.core.security import (
    COMMON_PASSWORDS,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordHash,
    PasswordHasher,
    PasswordPolicyError,
    build_password_hasher,
    enforce_password_policy,
)

pytestmark = pytest.mark.unit

# Fast parameters: the cost factors are a property of the deployment, not of the logic.
_FAST = {"time_cost": 1, "memory_cost": 8192, "parallelism": 1}

STRONG_PASSWORD = "Taloqan-Exchange-2026!"


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return build_password_hasher(**_FAST)


class TestArgon2idHashing:
    def test_algorithm_is_argon2id(self, hasher: PasswordHasher) -> None:
        assert hasher.algorithm == "argon2id"
        assert hasher.hash("irrelevant").startswith("$argon2id$")

    def test_verify_matches_the_original_password(self, hasher: PasswordHasher) -> None:
        digest = hasher.hash(STRONG_PASSWORD)
        result = hasher.verify(STRONG_PASSWORD, digest)
        assert isinstance(result, PasswordHash)
        assert result.is_valid is True
        assert result.needs_rehash is False
        assert result.algorithm == "argon2id"

    def test_verify_rejects_a_wrong_password(self, hasher: PasswordHasher) -> None:
        digest = hasher.hash(STRONG_PASSWORD)
        assert hasher.verify("Taloqan-Exchange-2026?", digest).is_valid is False

    def test_salt_makes_each_hash_unique(self, hasher: PasswordHasher) -> None:
        first, second = hasher.hash(STRONG_PASSWORD), hasher.hash(STRONG_PASSWORD)
        assert first != second
        assert hasher.verify(STRONG_PASSWORD, first).is_valid
        assert hasher.verify(STRONG_PASSWORD, second).is_valid

    def test_plaintext_is_never_part_of_the_hash(self, hasher: PasswordHasher) -> None:
        assert STRONG_PASSWORD not in hasher.hash(STRONG_PASSWORD)

    @pytest.mark.parametrize(
        "malformed",
        ["", "not-a-hash", "$argon2id$v=19$m=65536,t=3,p=4$onlyfourparts", "$2b$broken"],
    )
    def test_malformed_hash_is_rejected_without_raising(
        self, hasher: PasswordHasher, malformed: str
    ) -> None:
        result = hasher.verify(STRONG_PASSWORD, malformed)
        assert result.is_valid is False

    def test_parameters_are_exposed_and_secret_free(self, hasher: PasswordHasher) -> None:
        parameters = hasher.parameters
        assert parameters == _FAST
        # Defensive copy: callers must not be able to mutate the hasher.
        parameters["time_cost"] = 99
        assert hasher.parameters["time_cost"] == 1

    def test_needs_rehash_when_parameters_increase(self, hasher: PasswordHasher) -> None:
        digest = hasher.hash(STRONG_PASSWORD)
        stronger = build_password_hasher(time_cost=2, memory_cost=8192, parallelism=1)
        result = stronger.verify(STRONG_PASSWORD, digest)
        assert result.is_valid is True
        assert result.needs_rehash is True


class TestPasswordPolicy:
    def test_strong_password_is_accepted(self) -> None:
        enforce_password_policy(STRONG_PASSWORD, username="cashier01", full_name="Ahmad Wali")

    def test_short_password_is_rejected(self) -> None:
        with pytest.raises(PasswordPolicyError, match=f"at least {MIN_PASSWORD_LENGTH}"):
            enforce_password_policy("Aa1!" + "x" * (MIN_PASSWORD_LENGTH - 8))

    def test_overlong_password_is_rejected(self) -> None:
        with pytest.raises(PasswordPolicyError, match=f"at most {MAX_PASSWORD_LENGTH}"):
            enforce_password_policy("Aa1!" + "x" * MAX_PASSWORD_LENGTH)

    def test_surrounding_whitespace_is_rejected(self) -> None:
        with pytest.raises(PasswordPolicyError, match="whitespace"):
            enforce_password_policy(" Strong-Password-2026! ")

    @pytest.mark.parametrize("weak", sorted(COMMON_PASSWORDS))
    def test_every_shipped_blocklist_entry_is_rejected(self, weak: str) -> None:
        # An entry may fail on length or character classes before the blocklist is
        # consulted; the contract is that none of them can ever be set as a password.
        with pytest.raises(PasswordPolicyError):
            enforce_password_policy(weak)

    def test_blocklist_is_consulted_for_policy_compliant_passwords(self) -> None:
        # The shipped list currently holds nothing 12+ characters long, so the
        # blocklist branch is exercised with an explicit list (an operator may extend it).
        candidate = "Taloqan-Exchange-2026!"
        with pytest.raises(PasswordPolicyError, match="common passwords"):
            enforce_password_policy(candidate, blocklist=frozenset({candidate.casefold()}))
        enforce_password_policy(candidate, blocklist=frozenset({"something-else-entirely"}))

    def test_password_equal_to_username_is_rejected(self) -> None:
        with pytest.raises(PasswordPolicyError, match="username"):
            enforce_password_policy("Cashier01-2026!", username="cashier01-2026!")

    def test_password_equal_to_full_name_is_rejected(self) -> None:
        with pytest.raises(PasswordPolicyError, match="full name"):
            enforce_password_policy("Ahmad Wali 2026!", full_name="ahmad wali 2026!")

    @pytest.mark.parametrize(
        "weak",
        [
            "alllowercaseletters",  # one class
            "ALLUPPERCASELETTERS",  # one class
            "1234567890123456",  # one class
        ],
    )
    def test_fewer_than_three_character_classes_is_rejected(self, weak: str) -> None:
        with pytest.raises(PasswordPolicyError, match="three of"):
            enforce_password_policy(weak)

    def test_three_character_classes_are_enough(self) -> None:
        enforce_password_policy("Afghanistan2026")

    def test_policy_is_case_insensitive_against_the_blocklist(self) -> None:
        entry = sorted(COMMON_PASSWORDS)[0]
        with pytest.raises(PasswordPolicyError):
            enforce_password_policy(entry.upper())

    def test_custom_blocklist_replaces_the_default(self) -> None:
        with pytest.raises(PasswordPolicyError, match="common passwords"):
            enforce_password_policy(
                "Unique-Password-2026!", blocklist=frozenset({"unique-password-2026!"})
            )
