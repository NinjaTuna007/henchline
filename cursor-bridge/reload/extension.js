// Henchline's reload helper inside Cursor: each window listens on a private Unix socket for two requests.
// {"terminals": true} lists this window's own terminals (their top process's pid), so Henchline knows which window
// holds a terminal even while that window runs an older bridge that can't say. {"reload": true} runs Cursor's own
// Developer: Reload Window there, so the window loads a newer Henchline bridge. Henchline asks only when nothing
// in that window is working (no chat generating, no agent in its
// terminals mid-turn). Nothing else is accepted. A first install starts in running windows without a reload, which
// is why this is its own small extension: a window running an older bridge can be reloaded from outside.
// The socket is only the user's (0700 folder, 0600 file): ~/.local/state/ptt/cursor-bridge/<pid>.reload, the same
// pid as that window's bridge note (both run in the window's extension host).
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const vscode = require("vscode");

const DIR = path.join(os.homedir(), ".local/state/ptt/cursor-bridge");
let server = null;
let sock = "";

function withinASecond(promise) {
  return Promise.race([Promise.resolve(promise), new Promise((resolve) => setTimeout(() => resolve(undefined), 1000))]);
}

// This window's terminals that run on this computer as processes (not an extension's pseudoterminal, not remote)
async function localTerminals() {
  if (vscode.env.remoteName) return [];
  const found = [];
  for (const terminal of vscode.window.terminals) {
    if ((terminal.creationOptions || {}).pty) continue;
    let pid;
    try { pid = await withinASecond(terminal.processId); } catch (_) { pid = undefined; }
    if (Number.isSafeInteger(pid) && pid > 0) found.push({ pid, name: String(terminal.name || "") });
  }
  return found;
}

function activate(context) {
  fs.mkdirSync(DIR, { recursive: true, mode: 0o700 });
  sock = path.join(DIR, `${process.pid}.reload`);
  try { fs.unlinkSync(sock); } catch (_) {}
  server = net.createServer((conn) => {
    let buffered = "";
    let handled = false;
    conn.on("data", async (chunk) => {
      if (handled) return;
      buffered += chunk.toString();
      if (buffered.length > 256) { handled = true; conn.destroy(); return; }
      if (!buffered.includes("\n")) return;
      handled = true;
      let asked = null;
      try { asked = JSON.parse(buffered.split("\n")[0]); } catch (_) {}
      if (asked && asked.terminals === true) {
        conn.end(JSON.stringify({ ok: true, terminals: await localTerminals() }) + "\n");
        return;
      }
      if (!asked || asked.reload !== true) {
        conn.end(JSON.stringify({ ok: false, why: "not a reload" }) + "\n");
        return;
      }
      // Answered first: the reload ends this extension host, and with it the connection
      conn.end(JSON.stringify({ ok: true }) + "\n", () => {
        setTimeout(() => {
          vscode.commands.executeCommand("workbench.action.reloadWindow").then(undefined, () => {});
        }, 200);
      });
    });
    conn.on("error", () => {});
  });
  server.listen(sock, () => fs.chmodSync(sock, 0o600));
  context.subscriptions.push({ dispose: deactivate });
}

function deactivate() {
  if (server) { server.close(); server = null; }
  if (sock) { try { fs.unlinkSync(sock); } catch (_) {} }
  sock = "";
}

module.exports = { activate, deactivate };
