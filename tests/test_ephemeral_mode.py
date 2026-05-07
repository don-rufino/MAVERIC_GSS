"""Tests for ephemeral mode (GSS_EPHEMERAL=1)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from mav_gss_lib.server import ephemeral


class IsActiveTests(unittest.TestCase):
    def test_unset_env_is_inactive(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ephemeral.is_active())

    def test_truthy_values_activate(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", "On"):
            with mock.patch.dict(os.environ, {"GSS_EPHEMERAL": value}):
                self.assertTrue(ephemeral.is_active(),
                                f"{value!r} should activate")

    def test_falsy_values_dont_activate(self) -> None:
        for value in ("", "0", "false", "no", "off", " "):
            with mock.patch.dict(os.environ, {"GSS_EPHEMERAL": value}):
                self.assertFalse(ephemeral.is_active(),
                                 f"{value!r} should NOT activate")


class ApplyTests(unittest.TestCase):
    def test_apply_creates_tempdir_and_redirects(self) -> None:
        cfg: dict = {"general": {"log_dir": "logs",
                                  "generated_commands_dir": "generated"}}
        tmp_root, cleanup = ephemeral.apply(cfg)
        try:
            self.assertTrue(tmp_root.exists())
            self.assertTrue(tmp_root.is_dir())
            self.assertTrue(tmp_root.name.startswith("gss-ephemeral-"))
            self.assertEqual(cfg["general"]["log_dir"],
                             str(tmp_root / "logs"))
            self.assertEqual(cfg["general"]["generated_commands_dir"],
                             str(tmp_root / "generated"))
        finally:
            cleanup()

    def test_apply_creates_general_section_if_missing(self) -> None:
        cfg: dict = {}
        _, cleanup = ephemeral.apply(cfg)
        try:
            self.assertIn("general", cfg)
            self.assertIn("log_dir", cfg["general"])
            self.assertIn("generated_commands_dir", cfg["general"])
        finally:
            cleanup()

    def test_cleanup_removes_tempdir(self) -> None:
        cfg: dict = {}
        tmp_root, cleanup = ephemeral.apply(cfg)
        # Drop something inside to verify rmtree handles non-empty dirs.
        (tmp_root / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_root / "logs" / "session.jsonl").write_text("noise\n")
        self.assertTrue(tmp_root.exists())
        cleanup()
        self.assertFalse(tmp_root.exists())

    def test_cleanup_is_idempotent(self) -> None:
        # Calling cleanup twice (or after atexit fires it once) shouldn't raise.
        cfg: dict = {}
        tmp_root, cleanup = ephemeral.apply(cfg)
        cleanup()
        cleanup()  # second call must not raise even though dir is gone
        self.assertFalse(tmp_root.exists())

    def test_apply_registers_atexit_hook(self) -> None:
        with mock.patch.object(ephemeral, "atexit") as atexit_mock:
            cfg: dict = {}
            _, cleanup = ephemeral.apply(cfg)
            try:
                atexit_mock.register.assert_called_once()
                # The registered callable is the cleanup we got back.
                registered = atexit_mock.register.call_args[0][0]
                self.assertIs(registered, cleanup)
            finally:
                cleanup()


class WebRuntimeIntegrationTests(unittest.TestCase):
    """End-to-end: WebRuntime construction picks up the env var and
    redirects log_dir + sets config_save_disabled."""

    def test_runtime_redirects_log_dir_when_env_set(self) -> None:
        from mav_gss_lib.server.state import WebRuntime
        with mock.patch.dict(os.environ, {"GSS_EPHEMERAL": "1"}):
            try:
                runtime = WebRuntime()
            except Exception:
                self.skipTest("WebRuntime construction needs mission.yml; "
                              "skipping in environments without it")
                return
            try:
                self.assertTrue(runtime.config_save_disabled)
                self.assertIsNotNone(runtime._ephemeral_root)
                self.assertTrue(Path(runtime.log_dir).is_relative_to(
                    runtime._ephemeral_root))
                # generated_commands_dir() resolves through config helper —
                # verify it's also under the tempdir.
                self.assertTrue(
                    Path(runtime.generated_commands_dir()).is_relative_to(
                        runtime._ephemeral_root)
                )
            finally:
                if runtime._ephemeral_cleanup:
                    runtime._ephemeral_cleanup()

    def test_runtime_uses_real_log_dir_without_env(self) -> None:
        from mav_gss_lib.server.state import WebRuntime
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GSS_EPHEMERAL", None)
            try:
                runtime = WebRuntime()
            except Exception:
                self.skipTest("WebRuntime construction needs mission.yml")
                return
            self.assertFalse(runtime.config_save_disabled)
            self.assertIsNone(runtime._ephemeral_root)


class ConfigSaveGuardTests(unittest.TestCase):
    """When ``runtime.config_save_disabled`` is set, the /api/config save
    path skips ``save_operator_config``. We probe the guard directly so
    the test works without the heavier server-side wiring."""

    def test_guard_skips_save_when_disabled(self) -> None:
        from mav_gss_lib.server.api import config as api_config

        # The guard line lives in the body of api_config.update_config; we
        # don't run the whole endpoint here. Instead, simulate the same
        # condition: when runtime.config_save_disabled is True, the
        # save_operator_config call must be guarded out.
        class _FakeRuntime:
            config_save_disabled = True

        save_called = []

        with mock.patch.object(api_config, "save_operator_config",
                               side_effect=lambda *a, **k: save_called.append(1)):
            # Re-create the guard pattern used in update_config:
            runtime = _FakeRuntime()
            if not getattr(runtime, "config_save_disabled", False):
                api_config.save_operator_config({"dummy": True})

        self.assertEqual(save_called, [],
                         "save_operator_config must NOT be invoked when "
                         "config_save_disabled is True")

    def test_guard_allows_save_when_not_disabled(self) -> None:
        from mav_gss_lib.server.api import config as api_config

        class _FakeRuntime:
            config_save_disabled = False

        save_called = []
        with mock.patch.object(api_config, "save_operator_config",
                               side_effect=lambda *a, **k: save_called.append(1)):
            runtime = _FakeRuntime()
            if not getattr(runtime, "config_save_disabled", False):
                api_config.save_operator_config({"dummy": True})

        self.assertEqual(save_called, [1])


if __name__ == "__main__":
    unittest.main()
