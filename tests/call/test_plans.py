"""call/server.py: plan display and approval stay scoped to the chat's agent."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:  # the call's own packages are in the project's venv: run the suite with its python
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

spec = importlib.util.spec_from_file_location("call_server_plans", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class PlanRoutes(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_post("/chat/plan/build", server.chat_plan_build)
        app.router.add_get("/chat/plan", server.chat_plan)
        return app

    def origin(self):
        return {"Origin": f"https://{self.server.host}:{self.server.port}"}

    async def test_build_uses_the_same_session_owned_plan_as_the_chat(self):
        project = Path("/home/me/henchline")
        target = mock.Mock(session="codex-00000000-0000-0000-0000-000000000001")
        plan = {"name": "Codex plan", "file": "/home/me/.codex/plans/mine.plan.md"}
        with mock.patch.object(server, "recent_speakers", return_value=[target]), \
                mock.patch.object(server, "project_of", return_value=project), \
                mock.patch.object(server.plans, "of_session", return_value=plan) as owned, \
                mock.patch.object(server.plans, "find") as project_wide, \
                mock.patch.object(server, "to_agent", return_value=("Codex", "")) as to_agent:
            answer = await self.client.post("/chat/plan/build", json={"session": target.session},
                                            headers=self.origin())

        self.assertEqual(await answer.json(), {"to": "Codex"})
        owned.assert_called_once_with(target.session, project, None)
        project_wide.assert_not_called()
        to_agent.assert_called_once_with(server.plans.approval(plan), target.session, server.Heard.TEXTED, False)

    async def test_build_takes_the_plan_the_phone_shows(self):
        target = mock.Mock(session="cursor-00000000-0000-0000-0000-000000000001")
        plan = {"name": "B", "file": "/home/me/.cursor/plans/b.plan.md"}
        with mock.patch.object(server, "recent_speakers", return_value=[target]), \
                mock.patch.object(server, "project_of", return_value=None), \
                mock.patch.object(server.plans, "of_session", return_value=plan) as owned, \
                mock.patch.object(server, "to_agent", return_value=("Cursor", "")):
            answer = await self.client.post("/chat/plan/build", json={"session": target.session, "plan": plan["file"]},
                                            headers=self.origin())
        self.assertEqual(await answer.json(), {"to": "Cursor"})
        owned.assert_called_once_with(target.session, None, plan["file"])

    async def test_a_plan_the_chat_does_not_own_is_refused(self):
        target = mock.Mock(session="cursor-00000000-0000-0000-0000-000000000001")
        with mock.patch.object(server, "recent_speakers", return_value=[target]), \
                mock.patch.object(server, "project_of", return_value=None), \
                mock.patch.object(server.plans, "plans_for_session", return_value=[]), \
                mock.patch.object(server, "to_agent") as to_agent:
            shown = await self.client.get("/chat/plan", params={"session": target.session, "plan": "/etc/passwd"},
                                          headers=self.origin())
            built = await self.client.post("/chat/plan/build", json={"session": target.session, "plan": "/etc/passwd"},
                                           headers=self.origin())
        self.assertEqual(shown.status, 404)
        self.assertNotIn("todos", await shown.json())
        self.assertEqual(built.status, 404)
        to_agent.assert_not_called()

    async def test_the_plan_comes_with_the_chat_s_plans_and_old_requests_still_work(self):
        import tempfile

        folder = Path(tempfile.mkdtemp())
        a, b = folder / "a.plan.md", folder / "b.plan.md"
        a.write_text("---\nname: A\ntodos:\n  - id: x\n    content: one\n    status: completed\n---\n")
        b.write_text("---\nname: B\ntodos:\n  - id: x\n    content: one\n    status: pending\n---\n")
        target = mock.Mock(session="cursor-00000000-0000-0000-0000-000000000001")
        with mock.patch.object(server, "recent_speakers", return_value=[target]), \
                mock.patch.object(server, "project_of", return_value=None), \
                mock.patch.object(server.plans, "plans_for_session", return_value=[a, b]):
            old = await (await self.client.get("/chat/plan", params={"session": target.session},
                                               headers=self.origin())).json()
            picked = await (await self.client.get("/chat/plan", params={"session": target.session, "plan": str(b)},
                                                  headers=self.origin())).json()
        self.assertEqual(old["name"], "A")
        self.assertEqual(old["todos"][0]["status"], "completed")
        self.assertEqual([p["name"] for p in old["plans"]], ["A", "B"])
        self.assertEqual(old["plans"][1], {"file": str(b), "name": "B", "done": 0, "total": 1})
        self.assertEqual(picked["name"], "B")
        self.assertEqual(picked["file"], str(b))


if __name__ == "__main__":
    unittest.main()
