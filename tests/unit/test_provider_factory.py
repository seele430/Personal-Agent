"""Provider 工厂测试：按环境变量在方舟与默认端点之间切换。"""

import os
import unittest

from unittest.mock import patch

from src.collab.providers import ARK_BASE_URL, build_provider, provider_name


CLEAN = {"ARK_API_KEY": "", "ARK_BASE_URL": "", "ARK_MODEL": "", "MODEL": "", "OPENAI_API_KEY": ""}


def env(**overrides):
    values = dict(CLEAN)
    values.update(overrides)

    return patch.dict(os.environ, values, clear=False)


class FactoryCase(unittest.TestCase):
    def test_defaults_to_openai_compat(self):
        with env():
            self.assertEqual(provider_name(), "openai-compat")

    def test_ark_selected_when_key_present(self):
        with env(ARK_API_KEY="k" * 20):
            provider = build_provider()

            self.assertEqual(provider.name, "ark")
            self.assertEqual(provider.base_url, ARK_BASE_URL)
            self.assertEqual(provider.model, "doubao-seed-2-0-mini-260428")

    def test_ark_model_override(self):
        with env(ARK_API_KEY="k" * 20, ARK_MODEL="ep-2026-test"):
            self.assertEqual(build_provider().model, "ep-2026-test")

    def test_ark_falls_back_to_model_env(self):
        with env(ARK_API_KEY="k" * 20, MODEL="doubao-seed-2.0-lite"):
            self.assertEqual(build_provider().model, "doubao-seed-2.0-lite")

    def test_ark_base_url_override(self):
        with env(ARK_API_KEY="k" * 20, ARK_BASE_URL="https://example.invalid/v3"):
            self.assertEqual(build_provider().base_url, "https://example.invalid/v3")

    def test_whitespace_key_treated_as_empty(self):
        with env(ARK_API_KEY="   "):
            self.assertEqual(provider_name(), "openai-compat")


if __name__ == "__main__":
    unittest.main()
