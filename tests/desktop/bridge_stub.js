// The bridge's terminal requests run in node against a stub `vscode` module and a fake /proc (stat files only):
// `node bridge_stub.js EXTENSION_JS`, with HOME set to a throwaway folder. Exits non-zero on the first failure.
const assert = require("assert");
const fs = require("fs");
const Module = require("module");
const net = require("net");
const os = require("os");
const path = require("path");

const sent = [];  // [terminal name, text] for every sendText
function terminal(name, pid, options) {
  return { name, processId: Promise.resolve(pid), creationOptions: options || {},
           sendText: (text, execute) => { assert.strictEqual(execute, false); sent.push([name, text]); } };
}
const vscode = {
  env: { remoteName: undefined },
  window: { terminals: [terminal("bash", 300), terminal("pseudo", 301, { pty: {} }),
                        terminal("slow", 302), terminal("other", 310)] },
  workspace: { name: "ws", workspaceFolders: [] },
  commands: { executeCommand: async () => undefined, getCommands: async () => [] },
};
vscode.window.terminals[2].processId = new Promise(() => {});  // never resolves: skipped after a second

const realResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  return request === "vscode" ? "vscode" : realResolve.call(this, request, ...rest);
};
require.cache.vscode = { id: "vscode", filename: "vscode", loaded: true, exports: vscode };

// /proc/<pid>/stat: state ppid pgrp session tty tpgid … starttime (the 20th field after the name)
const procs = {};  // /proc/<pid>/stat → its text
const inputs = {};  // /proc/<pid>/fd/0 → where it points
function proc(pid, tpgid, start, { ppid = 1, pgrp = pid, stdin = "/dev/pts/1" } = {}) {
  const fields = ["S", ppid, pgrp, pid, 34817, tpgid, 0, 0, 0, 0, 0, 0, 0, 0, 0, 20, 0, 1, 0, start, 0];
  procs[`/proc/${pid}/stat`] = `${pid} (a b) ${fields.join(" ")}\n`;
  inputs[`/proc/${pid}/fd/0`] = stdin;
}
function gone(pid) {
  delete procs[`/proc/${pid}/stat`];
  delete inputs[`/proc/${pid}/fd/0`];
}
function missing() {
  const e = new Error("ENOENT");
  e.code = "ENOENT";
  return e;
}
const realReadlink = fs.readlinkSync;
fs.readlinkSync = function (file, ...rest) {
  if (typeof file === "string" && file.startsWith("/proc/")) {
    if (inputs[file] === undefined) throw missing();
    return inputs[file];
  }
  return realReadlink.call(this, file, ...rest);
};
const realReaddir = fs.readdirSync;
fs.readdirSync = function (dir, ...rest) {
  if (dir === "/proc") return [...new Set(Object.keys(procs).map((f) => f.split("/")[2]))].concat(["self", "net"]);
  return realReaddir.call(this, dir, ...rest);
};
const realRead = fs.readFileSync;
fs.readFileSync = function (file, ...rest) {
  if (typeof file === "string" && file.startsWith("/proc/")) {
    if (procs[file] === undefined) throw missing();
    return procs[file];
  }
  return realRead.call(this, file, ...rest);
};

const extension = require(path.resolve(process.argv[2]));
const { terminalRequest, localTerminals } = extension._test;
const check = { shell: 300, shell_born: 5000, front: 400, front_born: 6000, agent: 400, agent_born: 6000 };

function ask(sock, line) {
  return new Promise((resolve) => {
    const conn = net.createConnection(sock);
    let got = "";
    conn.on("data", (d) => { got += d; });
    conn.on("close", () => resolve(got));
    conn.on("error", () => resolve(got));
    conn.write(line);
  });
}

(async () => {
  proc(300, 400, 5000);
  proc(400, 400, 6000);

  // Only real, local terminals, each with its pid; one that never says its pid is left out
  assert.deepStrictEqual(await localTerminals(), [{ pid: 300, name: "bash" }, { pid: 310, name: "other" }]);
  vscode.env.remoteName = "ssh-remote";
  assert.deepStrictEqual(await localTerminals(), []);
  vscode.env.remoteName = undefined;

  // One bracketed paste with no control characters in it, then Enter
  let said = await terminalRequest({ type: { ...check, text: "one\r\n\ntwo \x1b[201~x\x07\ty", enter: true } });
  assert.deepStrictEqual(said, { ok: true });
  assert.deepStrictEqual(sent.splice(0), [["bash", "\x1b[200~one\n\ntwo [201~x\ty\x1b[201~"], ["bash", "\r"]]);

  // Another program in front, a reused pid, the shell gone: nothing written
  proc(300, 300, 5000);
  said = await terminalRequest({ type: { ...check, text: "rm -rf ~", enter: true } });
  assert.strictEqual(said.ok, false);
  assert.match(said.why, /another program is in front/);
  proc(300, 400, 5000);
  proc(400, 400, 6001);
  assert.strictEqual((await terminalRequest({ escape: check })).ok, false);
  proc(400, 400, 6000);
  proc(300, 400, 4999);
  assert.match((await terminalRequest({ enter: check })).why, /shell is gone/);
  proc(300, 400, 5000);
  assert.deepStrictEqual(sent, []);

  // Escape and Enter after the same check; a terminal of another window, a bad check, too many words: refused
  assert.deepStrictEqual(await terminalRequest({ escape: check }), { ok: true });
  assert.deepStrictEqual(await terminalRequest({ enter: check }), { ok: true });
  assert.deepStrictEqual(sent.splice(0), [["bash", "\x1b"], ["bash", "\r"]]);
  assert.match((await terminalRequest({ enter: { ...check, shell: 999 } })).why, /isn't in this window/);
  assert.match((await terminalRequest({ enter: { ...check, front: "400" } })).why, /not a terminal check/);
  assert.match((await terminalRequest({ type: { ...check, text: 7 } })).why, /no words/);
  assert.deepStrictEqual(sent, []);

  // A wrapper in front: its agent child must still be there, in its group (`sh -c 'claude; exec bash'`)
  const wrapped = { ...check, front: 390, front_born: 5900 };
  proc(300, 390, 5000);
  proc(390, 390, 5900, { ppid: 300 });
  proc(400, 390, 6000, { ppid: 390, pgrp: 390 });
  assert.deepStrictEqual(await terminalRequest({ enter: wrapped }), { ok: true });
  gone(400);  // the agent exited; the wrapper's shell is about to take the terminal
  assert.match((await terminalRequest({ type: { ...wrapped, text: "rm -rf ~", enter: true } })).why,
               /agent isn't in front/);
  proc(400, 390, 6000, { ppid: 1, pgrp: 390 });  // the same agent, but no longer the wrapper's child
  assert.match((await terminalRequest({ enter: wrapped })).why, /agent isn't in front/);
  proc(400, 400, 6000, { ppid: 390 });  // in a group of its own
  assert.match((await terminalRequest({ enter: wrapped })).why, /agent isn't in front/);
  assert.deepStrictEqual(sent.splice(0), [["bash", "\r"]]);
  gone(390);
  proc(300, 400, 5000);
  proc(400, 400, 6000);

  // Another program in the agent's group reading the terminal (an editor it opened): nothing written
  proc(450, 400, 6500, { ppid: 400, pgrp: 400 });
  assert.match((await terminalRequest({ enter: check })).why, /reading that terminal/);
  proc(450, 400, 6500, { ppid: 400, pgrp: 400, stdin: "pipe:[7]" });  // its own pipe: fine
  proc(460, 460, 6600, { ppid: 400 });  // another group on the terminal: not in front
  assert.deepStrictEqual(await terminalRequest({ enter: check }), { ok: true });
  assert.deepStrictEqual(sent.splice(0), [["bash", "\r"]]);
  gone(450);
  gone(460);

  // Two messages to one terminal at once: each paste with its own Enter, never interleaved
  await Promise.all([terminalRequest({ type: { ...check, text: "a", enter: true } }),
                     terminalRequest({ type: { ...check, text: "b", enter: true } })]);
  assert.deepStrictEqual(sent.splice(0).map((s) => s[1]), ["\x1b[200~a\x1b[201~", "\r", "\x1b[200~b\x1b[201~", "\r"]);

  // The program in front changes between the words and their Enter: the Enter isn't sent
  const typing = terminalRequest({ type: { ...check, text: "hi", enter: true } });
  setTimeout(() => proc(300, 300, 5000), 20);
  said = await typing;
  assert.strictEqual(said.ok, false);
  assert.match(said.why, /not sent/);
  assert.deepStrictEqual(sent.splice(0), [["bash", "\x1b[200~hi\x1b[201~"]]);
  proc(300, 400, 5000);

  // Over its socket: the note says its version, chat requests keep their 4 KB line, words may be longer
  const subscriptions = [];
  extension.activate({ subscriptions });
  const dir = path.join(os.homedir(), ".local/state/ptt/cursor-bridge");
  const sock = path.join(dir, `${process.pid}.sock`);
  for (let i = 0; i < 50 && !fs.existsSync(path.join(dir, `${process.pid}.json`)); i++) {
    await new Promise((r) => setTimeout(r, 20));
  }
  const note = JSON.parse(realRead(path.join(dir, `${process.pid}.json`), "utf8"));
  const shipped = require(path.join(path.dirname(path.resolve(process.argv[2])), "package.json")).version;
  assert.strictEqual(note.version, shipped);
  const listed = JSON.parse(await ask(sock, JSON.stringify({ terminals: true }) + "\n"));
  assert.strictEqual(listed.ok, true);
  assert.deepStrictEqual(listed.terminals.map((t) => t.pid), [300, 310]);
  assert.match(JSON.parse(await ask(sock, JSON.stringify({ open: "nope" }) + "\n")).why, /not a chat id/);
  assert.strictEqual(await ask(sock, JSON.stringify({ open: "x".repeat(5000) }) + "\n"), "");
  // Bytes, not characters: a chat request's line over 4 KB in UTF-8 is cut off, a long message is not
  assert.strictEqual(await ask(sock, JSON.stringify({ open: "\u00e9".repeat(2100) }) + "\n"), "");  // 4.2 KB
  const long = "word ".repeat(4000) + "\u00e9\u4e2d";
  said = JSON.parse(await ask(sock, JSON.stringify({ type: { ...check, text: long, enter: true } }) + "\n"));
  assert.deepStrictEqual(said, { ok: true });
  assert.strictEqual(sent.splice(0)[0][1], "\x1b[200~" + long + "\x1b[201~");
  extension.deactivate();
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
