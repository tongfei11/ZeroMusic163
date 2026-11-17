import fcntl
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import uninstall


class UninstallTest(unittest.TestCase):
    def setUp(self):
        output = patch("builtins.print")
        output.start()
        self.addCleanup(output.stop)

    def test_symlinked_profile_does_not_delete_external_data(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            external = home / "external"
            external.mkdir()
            (external / "keep").write_text("unrelated data")
            root = home / ".local/share/zero-music163"
            root.mkdir(parents=True)
            (root / "profile").symlink_to(external)
            with patch("uninstall.Path.home", return_value=home), patch("uninstall.subprocess.run") as remove:
                self.assertEqual(uninstall.main(), 1)
                remove.assert_not_called()
            self.assertTrue((external / "keep").exists())

    def test_full_uninstall_removes_developer_configuration_and_runtime(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / ".local/share/zero-music163"
            account = root / "profile/account-test"
            account.mkdir(parents=True)
            (account / "credentials").write_text("local test credential")
            (root / "profile/current").symlink_to(account.name)
            (root / "runtime").mkdir()
            with patch("uninstall.Path.home", return_value=home), patch("uninstall.subprocess.run") as remove:
                self.assertEqual(uninstall.main(), 0)
                self.assertIn("/usr/local/lib/zero-music163", remove.call_args.args[0])
            self.assertFalse(root.exists())

    def test_active_ui_prevents_removing_its_data(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            profile = home / ".local/share/zero-music163/profile"
            profile.mkdir(parents=True)
            with (profile / ".ui-lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with patch("uninstall.Path.home", return_value=home), patch("uninstall.subprocess.run") as remove:
                    self.assertEqual(uninstall.main(), 1)
                    remove.assert_not_called()
            self.assertTrue(profile.exists())
