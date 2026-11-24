// Henchline's bridge inside Cursor: each window listens on a private Unix socket and takes a few requests about
// one agent chat, by its id: {"open"} brings it to the front (Cursor's composer.openComposer), {"cycle"} moves it
// to its next model (composer.cycleModel), {"stop"} stops what it's doing (composer.cancelComposerStep),
// {"model", "config"} sets its model
// (cursorai.action.switchToModelSlug). Henchline writes into the chat itself (through Cursor's debugging port,
// else with keys). No text and no other command reaches a chat.
// Since 0.2.0, also about this window's own terminals (an agent CLI started in Cursor's terminal panel):
// {"terminals"} lists them (their top process's pid), and {"type"}, {"enter"}, {"escape"} write into one, but
// only while the program Henchline names (pid and start time) is in front of it, checked here right before each
// write. A shell or any other program in front: nothing is written. The words go as one bracketed paste with its
// control characters taken out, then Enter, as tmux types them.
// The socket and its note are only the user's (0700 folder, 0600 files), under ~/.local/state/ptt/cursor-bridge/.
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const vscode = require("vscode");

const DIR = path.join(os.homedir(), ".local/state/ptt/cursor-bridge");
const CHAT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const VERSION = require("./package.json").version;
const LINE = 4096;  // bytes: one short line is all a request about a chat ever takes
const TYPE_LINE = 1 << 20;  // bytes of a {"type"} request's line, which carries the words
// Control characters a terminal or a TUI would act on instead of showing: C0 except tab and newline, DEL, and C1.
// ESC above all: "ESC[201~" would end the paste early and type the rest as keys
const CONTROLS = /[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]/g;
let server = null;
let files = [];

// Cursor's stop and model commands act on its selected chat: the most recently focused of those open. Asked with
// {"focused": true}, Henchline has just focused this chat's box through Cursor's debugging port and seen it hold
// focus, so it is the selected one wherever its pane is; then it need only be open. Without
// that, the old rule: the chat must be the last listed (refuses a chat in the left pane, never the wrong one).
async function actsOn(chat, focused) {
  if (!focused) await vscode.commands.executeCommand("composer.openComposer", chat);
  const selected = await vscode.commands.executeCommand("composer.getOrderedSelectedComposerIds");
  const listed = Array.isArray(selected) ? selected : [];
  return { ok: focused ? listed.includes(chat) : listed[listed.length - 1] === chat, selected };
}

// /proc/<pid>/stat after the program's name: [state, ppid, pgrp, session, tty, tpgid, …, starttime at 19]
function stat(pid) {
  try {
    const text = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    return text.slice(text.lastIndexOf(")") + 2).split(" ");
  } catch (_) {
    return null;
  }
}

function whole(n) {
  return Number.isSafeInteger(n) && n > 0;
}

// The terminal's shell, the program in front of it and the agent are still the ones Henchline checked: the shell
// (same start time) has that program's process group in front; the agent (same start time) is in that group and is
// that program or its child; and nothing else in that group reads the terminal (an editor the agent opened, say).
// "" when so, else why. Asked right before every write.
function stillInFront(check) {
  const shell = stat(check.shell);
  if (!shell || shell[19] !== String(check.shell_born)) return "that terminal's shell is gone";
  if (shell[5] !== String(check.front)) return "another program is in front";
  const front = stat(check.front);
  if (!front || front[19] !== String(check.front_born)) return "another program is in front";
  const agent = stat(check.agent);
  if (!agent || agent[19] !== String(check.agent_born) || agent[2] !== String(check.front) ||
      (check.agent !== check.front && agent[1] !== String(check.front))) {
    return "the agent isn't in front of that terminal any more";
  }
  let line;
  try { line = fs.readlinkSync(`/proc/${check.shell}/fd/0`); } catch (_) { return "that terminal can't be checked"; }
  let pids = [];
  try { pids = fs.readdirSync("/proc"); } catch (_) { return "that terminal can't be checked"; }
  for (const name of pids) {
    if (!/^\d+$/.test(name)) continue;
    const pid = Number(name);
    if (pid === check.front || pid === check.agent) continue;
    const other = stat(pid);
    if (!other || other[2] !== String(check.front)) continue;
    let input = "";
    try { input = fs.readlinkSync(`/proc/${pid}/fd/0`); } catch (_) { continue; }
    if (input === line) return "another program the agent started is reading that terminal";
  }
  return "";
}

function validCheck(check) {
  return check && whole(check.shell) && whole(check.shell_born) && whole(check.front) && whole(check.front_born) &&
    whole(check.agent) && whole(check.agent_born);
}

function withinASecond(promise) {
  return Promise.race([Promise.resolve(promise), new Promise((resolve) => setTimeout(() => resolve(undefined), 1000))]);
}

// This window's terminals that run on this computer as processes: pseudoterminals (an extension's own) have none,
// and a remote window's pids are another computer's
async function localTerminals() {
  if (vscode.env.remoteName) return [];
  const found = [];
  for (const terminal of vscode.window.terminals) {
    const options = terminal.creationOptions || {};
    if (options.pty) continue;
    let pid;
    try { pid = await withinASecond(terminal.processId); } catch (_) { pid = undefined; }
    if (whole(pid)) found.push({ pid, name: String(terminal.name || "") });
  }
  return found;
}

async function terminalOf(shell) {
  if (vscode.env.remoteName) return undefined;
  for (const terminal of vscode.window.terminals) {
    if ((terminal.creationOptions || {}).pty) continue;
    let pid;
    try { pid = await withinASecond(terminal.processId); } catch (_) { pid = undefined; }
    if (pid === shell) return terminal;
  }
  return undefined;
}

function pause(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Requests for one terminal run one after another: two messages' pastes and Enters never interleave
const queues = new Map();
function oneAtATime(shell, work) {
  const next = (queues.get(shell) || Promise.resolve()).then(work, work);
  const settled = next.then(() => {}, () => {});
  queues.set(shell, settled);
  settled.then(() => { if (queues.get(shell) === settled) queues.delete(shell); });
  return next;
}

// One of {"type"}, {"enter"}, {"escape"}: the answer to send back
function terminalRequest(asked) {
  const kind = ["type", "enter", "escape"].find((k) => asked[k] !== undefined);
  const check = asked[kind];
  if (!validCheck(check)) return Promise.resolve({ ok: false, why: "not a terminal check" });
  return oneAtATime(check.shell, () => writeTerminal(kind, check));
}

async function writeTerminal(kind, check) {
  if (kind === "type" && typeof check.text !== "string") return { ok: false, why: "no words" };
  const terminal = await terminalOf(check.shell);
  if (!terminal) return { ok: false, why: "that terminal isn't in this window" };
  let why = stillInFront(check);
  if (why) return { ok: false, why };
  if (kind === "escape") { terminal.sendText("\x1b", false); return { ok: true }; }
  if (kind === "enter") { terminal.sendText("\r", false); return { ok: true }; }
  const words = check.text.replace(/\r\n?/g, "\n").replace(CONTROLS, "");
  terminal.sendText("\x1b[200~" + words + "\x1b[201~", false);
  if (check.enter !== true) return { ok: true };
  await pause(words.length > 2048 ? 300 : 150);  // the agent takes the paste in before its Enter
  why = stillInFront(check);
  if (why) return { ok: false, why: why + ": the words were written but not sent" };
  terminal.sendText("\r", false);
  return { ok: true };
}

function activate(context) {
  fs.mkdirSync(DIR, { recursive: true, mode: 0o700 });
  const base = path.join(DIR, String(process.pid));
  const sock = base + ".sock";
  try { fs.unlinkSync(sock); } catch (_) {}
  server = net.createServer((conn) => {
    const chunks = [];
    let size = 0;
    let handled = false;  // one request per connection: whatever follows its line is ignored
    conn.on("data", async (chunk) => {
      if (handled) return;
      chunks.push(chunk);
      size += chunk.length;
      if (size > (chunks[0].toString("latin1", 0, 7) === '{"type"' ? TYPE_LINE : LINE)) {
        handled = true;
        conn.destroy();
        return;
      }
      if (!chunk.includes(10)) return;
      handled = true;
      const buffered = Buffer.concat(chunks).toString("utf8");  // whole, so no character is cut between chunks
      let answer;
      try {
        const asked = JSON.parse(buffered.split("\n")[0]);
        if (asked && asked.terminals === true) {
          conn.end(JSON.stringify({ ok: true, version: VERSION, terminals: await localTerminals() }) + "\n");
          return;
        }
        if (asked && (asked.type !== undefined || asked.enter !== undefined || asked.escape !== undefined)) {
          conn.end(JSON.stringify(await terminalRequest(asked)) + "\n");
          return;
        }
        if (typeof asked.commands === "string" && /^[a-z]{2,20}$/i.test(asked.commands)) {
          // Which of Cursor's commands an extension can reach that name a word ("model", "rename"): to find a way
          // to do something from the phone. Their names only: nothing is run
          const word = asked.commands.toLowerCase();
          const all = await vscode.commands.getCommands(true);
          conn.end(JSON.stringify({ ok: true, commands: all.filter((c) => c.toLowerCase().includes(word)) }) + "\n");
          return;
        }
        if (typeof asked.stop === "string" && CHAT_ID.test(asked.stop)) {
          // The phone's stop button: that chat stops what it's doing, as its own stop button does
          // Cursor's stop takes no chat id and works on the selected chat, which must be this one
          const check = await actsOn(asked.stop, asked.focused === true);
          if (!check.ok) {
            conn.end(JSON.stringify({ ok: false, why: "another chat is selected", selected: check.selected }) + "\n");
            return;
          }
          await vscode.commands.executeCommand("composer.cancelComposerStep");
          conn.end(JSON.stringify({ ok: true }) + "\n");
          return;
        }
        if (typeof asked.model === "string" && CHAT_ID.test(asked.model) && asked.config && typeof asked.config.modelId === "string") {
          // A chat's model, by Cursor's own "Switch to Model Slug": it sets the selected chat's model and, as a
          // change made in Cursor itself does, the default for new chats
          const check = await actsOn(asked.model, asked.focused === true);
          if (!check.ok) {
            conn.end(JSON.stringify({ ok: false, why: "another chat is selected", selected: check.selected }) + "\n");
            return;
          }
          const config = { modelId: String(asked.config.modelId), params: Array.isArray(asked.config.params) ? asked.config.params : [] };
          await vscode.commands.executeCommand("cursorai.action.switchToModelSlug", { modelIdWithParams: JSON.stringify(config) });
          conn.end(JSON.stringify({ ok: true }) + "\n");
          return;
        }
        if (typeof asked.cycle === "string" && CHAT_ID.test(asked.cycle)) {
          // The chat's next model, as Cursor's own "cycle model" does: no keys, no text; Henchline reads the model
          // it lands on from Cursor's store and asks again until it's the one picked on the phone
          await vscode.commands.executeCommand("composer.openComposer", asked.cycle);
          // Cycle only when that chat is the one Cursor has selected now: its "cycle model" takes no chat id and
          // works on the selected one, which must not be another chat
          const selected = await vscode.commands.executeCommand("composer.getOrderedSelectedComposerIds");
          const first = Array.isArray(selected) ? selected[selected.length - 1] : undefined;
          if (first !== asked.cycle) {
            conn.end(JSON.stringify({ ok: false, why: "another chat is selected", selected }) + "\n");
            return;
          }
          await vscode.commands.executeCommand("composer.cycleModel");
          conn.end(JSON.stringify({ ok: true }) + "\n");
          return;
        }
        if (typeof asked.rename === "string" && CHAT_ID.test(asked.rename) && typeof asked.name === "string" &&
            asked.name.trim().length > 0 && asked.name.length <= 120) {
          // The phone's /rename for an editor chat: Cursor's own rename, given the chat and its new name. Henchline
          // reads the name back from Cursor's store to know it took
          // It opens Cursor's own "Chat name" box and waits there: not awaited, Henchline fills the box in
          vscode.commands.executeCommand("composer.renameChat", asked.rename).then(undefined, () => {});
          conn.end(JSON.stringify({ ok: true }) + "\n");
          return;
        }
        if (typeof asked.open !== "string" || !CHAT_ID.test(asked.open)) throw new Error("not a chat id");
        await vscode.commands.executeCommand("composer.openComposer", asked.open);
        // The chat words will be typed into must be the selected one now (Henchline picked this window by the
        // chat's transcript; a chat in another window, or focus moved, says no)
        // (Cursor lists every chat open side by side, in pane order, not the focused one last: a second chat
        // open beside it refused every text to the first, 2026-11-16. openComposer has just made this one the
        // selected chat in its pane, so it must be among them.)
        // {"strict": true} (Henchline about to type with keys, which go to the focused chat) keeps the old rule:
        // the last listed. Sending through Cursor's debugging port finds the box by id, so it needs only "open".
        const selected = await vscode.commands.executeCommand("composer.getOrderedSelectedComposerIds");
        const listed = Array.isArray(selected) ? selected : [];
        const open = asked.strict === true ? listed[listed.length - 1] === asked.open : listed.includes(asked.open);
        answer = open ? { ok: true } : { ok: false, why: "another chat is selected", selected };
      } catch (e) {
        answer = { ok: false, why: String(e && e.message ? e.message : e) };
      }
      conn.end(JSON.stringify(answer) + "\n");
    });
    conn.on("error", () => {});
  });
  server.listen(sock, () => {
    fs.chmodSync(sock, 0o600);
    const folders = (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
    fs.writeFileSync(base + ".json", JSON.stringify({ pid: process.pid, sock, version: VERSION,
      workspace: vscode.workspace.name || "", folders }), { mode: 0o600 });
  });
  files = [sock, base + ".json"];
  context.subscriptions.push({ dispose: deactivate });
}

function deactivate() {
  if (server) { server.close(); server = null; }
  for (const f of files) { try { fs.unlinkSync(f); } catch (_) {} }
  files = [];
}

module.exports = { activate, deactivate, _test: { stillInFront, terminalRequest, localTerminals } };
