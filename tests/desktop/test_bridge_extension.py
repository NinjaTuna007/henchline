"""The bridge extension's terminal requests, run in node with a stub vscode module (tests/desktop/bridge_stub.js)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import sandbox  # noqa: F401

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parents[1] / "cursor-bridge"


@unittest.skipUnless(shutil.which("node"), "node isn't installed")
class BridgeInNode(unittest.TestCase):
    def run_stub(self, extension: Path) -> subprocess.CompletedProcess:
        home = tempfile.mkdtemp(prefix="hb-", dir="/tmp")  # the socket path must stay short
        self.addCleanup(shutil.rmtree, home, True)
        return subprocess.run(["node", str(HERE / "bridge_stub.js"), str(extension)], capture_output=True,
                              text=True, timeout=60, env={**os.environ, "HOME": home})

    def test_terminal_requests(self):
        done = self.run_stub(BRIDGE / "extension.js")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertIn("ok", done.stdout)

    def test_the_reload_helper_reloads_only_when_asked_to(self):
        home = tempfile.mkdtemp(prefix="hb-", dir="/tmp")
        self.addCleanup(shutil.rmtree, home, True)
        done = subprocess.run(["node", "-e", RELOAD_STUB, str(BRIDGE / "reload/extension.js")], capture_output=True,
                              text=True, timeout=30, env={**os.environ, "HOME": home})
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(done.stdout.split(), ['{"ok":true,"terminals":[{"pid":300,"name":"bash"}]}',
                                               '{"ok":false,"why":"not', 'a', 'reload"}', '{"ok":true}',
                                               "workbench.action.reloadWindow"])


RELOAD_STUB = r"""
const Module = require("module"), net = require("net"), path = require("path"), os = require("os");
const ran = [];
const resolve = Module._resolveFilename;
Module._resolveFilename = function (r, ...rest) { return r === "vscode" ? "vscode" : resolve.call(this, r, ...rest); };
require.cache.vscode = { id: "vscode", filename: "vscode", loaded: true,
  exports: { commands: { executeCommand: async (c) => { ran.push(c); } }, env: {},
             window: { terminals: [{ name: "bash", processId: Promise.resolve(300), creationOptions: {} },
                                   { name: "pty", processId: Promise.resolve(301),
                                     creationOptions: { pty: {} } }] } } };
const helper = require(path.resolve(process.argv[1]));
helper.activate({ subscriptions: [] });
const sock = path.join(os.homedir(), ".local/state/ptt/cursor-bridge", process.pid + ".reload");
const ask = (line) => new Promise((done) => { const c = net.createConnection(sock); let got = "";
  c.on("data", (d) => { got += d; }); c.on("close", () => done(got.trim())); c.write(line); });
setTimeout(async () => {
  console.log(await ask('{"terminals": true}\n'));
  console.log(await ask('{"reload": "yes"}\n'));
  if (ran.length) throw new Error("reloaded");
  console.log(await ask('{"reload": true}\n'));
  setTimeout(() => { console.log(ran.join(" ")); helper.deactivate(); }, 400);
}, 100);
"""


if __name__ == "__main__":
    unittest.main()
