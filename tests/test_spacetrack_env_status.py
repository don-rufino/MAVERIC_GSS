import os
import unittest
from unittest import mock

from mav_gss_lib.server.tracking.fetch_service import TleFetchService


class SpacetrackEnvStatusTests(unittest.TestCase):
    def test_identity_only(self):
        with mock.patch.dict(os.environ, {"SPACETRACK_IDENTITY": "u"}, clear=True):
            self.assertEqual(TleFetchService.spacetrack_env_status(),
                             {"identity_set": True, "password_set": False})

    def test_both_present(self):
        with mock.patch.dict(os.environ, {"SPACETRACK_IDENTITY": "u", "SPACETRACK_PASSWORD": "p"}, clear=True):
            self.assertEqual(TleFetchService.spacetrack_env_status(),
                             {"identity_set": True, "password_set": True})

    def test_neither(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(TleFetchService.spacetrack_env_status(),
                             {"identity_set": False, "password_set": False})


if __name__ == "__main__":
    unittest.main()
