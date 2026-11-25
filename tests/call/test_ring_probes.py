"""The app says whether it answers call probes along with its push address: only then is it asked (an older build
would ring the phone for a probe), and a body without the flag works as before."""

import importlib.util
import unittest
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)
from voice import state

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:  # the call's own packages are in the project's venv: run the suite with its python
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

spec = importlib.util.spec_from_file_location("call_server_probes", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

ADDRESS = "https://ntfy.sh/up-test"


class KeepsTheFlag(unittest.TestCase):
    def setUp(self):
        state.STATE.mkdir(parents=True, exist_ok=True)
        for path in (state.CALL_RING, state.CALL_RING_PROBES):
            path.unlink(missing_ok=True)
            self.addCleanup(path.unlink, missing_ok=True)
        patcher = mock.patch.object(server, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_with_the_flag_the_address_is_kept_as_understanding_probes(self):
        self.assertTrue(server.keep_ring_endpoint(ADDRESS, probes=True))
        self.assertEqual(state.CALL_RING.read_text(), ADDRESS)
        self.assertEqual(state.CALL_RING_PROBES.read_text(), ADDRESS)
        self.assertEqual(state.CALL_RING_PROBES.stat().st_mode & 0o777, 0o600)

    def test_without_it_as_before_and_any_earlier_flag_goes(self):
        server.keep_ring_endpoint(ADDRESS, probes=True)
        self.assertTrue(server.keep_ring_endpoint(ADDRESS))  # the same address, said by an older build
        self.assertEqual(state.CALL_RING.read_text(), ADDRESS)
        self.assertFalse(state.CALL_RING_PROBES.exists())

    def test_a_refused_address_changes_nothing(self):
        server.keep_ring_endpoint(ADDRESS, probes=True)
        self.assertFalse(server.keep_ring_endpoint("https://example.com/up", probes=True))
        self.assertEqual(state.CALL_RING_PROBES.read_text(), ADDRESS)


FLAGS = [({"probe": True}, True), ({}, False), ({"probe": "yes"}, False), ({"probe": 1}, False),
         ({"probe": False}, False)]


class TheRoute(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_post("/ring-endpoint", server.ring_endpoint)
        return app

    async def test_only_a_true_flag_says_probes(self):
        for extra, probes in FLAGS:
            with self.subTest(extra=extra), mock.patch.object(server, "trusted"), \
                    mock.patch.object(server, "keep_ring_endpoint", return_value=True) as keep:
                answer = await self.client.post("/ring-endpoint", json={"endpoint": ADDRESS, **extra})
                self.assertEqual(answer.status, 200)
                keep.assert_called_once_with(ADDRESS, probes)


class TheCallsMessage(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_true_flag_says_probes(self):
        for extra, probes in FLAGS:
            with self.subTest(extra=extra), mock.patch.object(server, "keep_ring_endpoint") as keep:
                await server.from_page({"ring_endpoint": ADDRESS, **extra}, mock.Mock())
                keep.assert_called_once_with(ADDRESS, probes)


if __name__ == "__main__":
    unittest.main()
