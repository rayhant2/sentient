import base64
import json
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from security.credentials import (
    CredentialCipher,
    CredentialConfigurationError,
    CredentialDecryptionError,
    CredentialEncryptionError,
)


def encryption_key() -> str:
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")


class CredentialCipherTests(unittest.TestCase):
    def setUp(self):
        self.key = encryption_key()
        self.cipher = CredentialCipher(active_key=self.key, active_key_id="primary-v1")

    def test_round_trip_returns_masked_secret(self):
        envelope = self.cipher.encrypt(
            "sk-user-secret",
            user_id="user-1",
            provider="anthropic",
        )

        decrypted = self.cipher.decrypt(
            envelope,
            user_id="user-1",
            provider="anthropic",
        )

        self.assertIsInstance(decrypted, SecretStr)
        self.assertEqual(decrypted.get_secret_value(), "sk-user-secret")
        self.assertNotIn("sk-user-secret", envelope)
        self.assertNotIn("AES", envelope)

    def test_repeated_encryption_uses_unique_nonces(self):
        first = self.cipher.encrypt("same-key", user_id="user-1", provider="anthropic")
        second = self.cipher.encrypt("same-key", user_id="user-1", provider="anthropic")

        self.assertNotEqual(first, second)
        self.assertNotEqual(json.loads(first)["n"], json.loads(second)["n"])

    def test_tampered_ciphertext_is_rejected(self):
        envelope = json.loads(
            self.cipher.encrypt("secret", user_id="user-1", provider="anthropic")
        )
        ciphertext = bytearray(base64.urlsafe_b64decode(envelope["ct"]))
        ciphertext[0] ^= 1
        envelope["ct"] = base64.urlsafe_b64encode(ciphertext).decode("ascii")

        with self.assertRaises(CredentialDecryptionError):
            self.cipher.decrypt(
                json.dumps(envelope),
                user_id="user-1",
                provider="anthropic",
            )

    def test_wrong_user_provider_or_key_is_rejected(self):
        envelope = self.cipher.encrypt(
            "secret",
            user_id="user-1",
            provider="anthropic",
        )

        cases = [
            (self.cipher, "user-2", "anthropic"),
            (self.cipher, "user-1", "openai"),
            (
                CredentialCipher(active_key=encryption_key(), active_key_id="primary-v1"),
                "user-1",
                "anthropic",
            ),
        ]
        for cipher, user_id, provider in cases:
            with self.subTest(user_id=user_id, provider=provider):
                with self.assertRaises(CredentialDecryptionError):
                    cipher.decrypt(envelope, user_id=user_id, provider=provider)

    def test_previous_key_supports_rotation(self):
        old = CredentialCipher(active_key=self.key, active_key_id="old-v1")
        envelope = old.encrypt("secret", user_id="user-1", provider="anthropic")
        rotated_cipher = CredentialCipher(
            active_key=encryption_key(),
            active_key_id="primary-v2",
            previous_keys={"old-v1": self.key},
        )

        rotated = rotated_cipher.rotate(
            envelope,
            user_id="user-1",
            provider="anthropic",
        )

        self.assertEqual(json.loads(rotated)["kid"], "primary-v2")
        self.assertEqual(
            rotated_cipher.decrypt(
                rotated,
                user_id="user-1",
                provider="anthropic",
            ).get_secret_value(),
            "secret",
        )

    def test_malformed_envelope_and_blank_key_fail_safely(self):
        with self.assertRaises(CredentialDecryptionError):
            self.cipher.decrypt("not-json", user_id="user-1", provider="anthropic")
        with self.assertRaises(CredentialEncryptionError):
            self.cipher.encrypt("  ", user_id="user-1", provider="anthropic")

    def test_invalid_master_key_is_rejected(self):
        with self.assertRaises(CredentialConfigurationError):
            CredentialCipher(active_key="not-a-key", active_key_id="primary-v1")


if __name__ == "__main__":
    unittest.main()
