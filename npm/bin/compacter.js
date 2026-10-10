#!/usr/bin/env node
// Thin launcher: compacter is a Python tool. This finds Python 3.10+, installs the PyPI package
// `compacter-agent` into a private virtualenv the first time (and after upgrades), then runs it.
"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const VERSION = require("../package.json").version;
const PYPI_NAME = "compacter-agent";
const GIT_URL = "git+https://github.com/Souravrajvi0/compacter.git";
const HOME = process.env.COMPACTER_NPM_HOME || path.join(os.homedir(), ".compacter", "npm-venv");
const IS_WIN = process.platform === "win32";
const VENV_PY = path.join(HOME, IS_WIN ? "Scripts" : "bin", IS_WIN ? "python.exe" : "python");
const MARKER = path.join(HOME, "installed-version");

function fail(msg) {
  process.stderr.write(`compacter: ${msg}\n`);
  process.exit(1);
}

function run(cmd, args, opts = {}) {
  return spawnSync(cmd, args, { encoding: "utf8", ...opts });
}

function findPython() {
  const candidates = IS_WIN
    ? [["py", ["-3"]], ["python", []], ["python3", []]]
    : [["python3", []], ["python", []]];
  for (const [cmd, pre] of candidates) {
    const r = run(cmd, [...pre, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"]);
    if (r.status !== 0) continue;
    const [maj, min] = r.stdout.trim().split(".").map(Number);
    if (maj === 3 && min >= 10) return [cmd, pre];
  }
  return null;
}

function pipInstall(py, spec) {
  return run(py, ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", spec], {
    stdio: ["ignore", "ignore", "pipe"],
  });
}

function ensureInstalled() {
  let installed = null;
  try { installed = fs.readFileSync(MARKER, "utf8").trim(); } catch (_) { /* first run */ }
  if (installed === VERSION && fs.existsSync(VENV_PY)) return;

  const found = findPython();
  if (!found) fail("Python 3.10 or newer is required but was not found. Install it from https://www.python.org/downloads/ and try again.");
  const [cmd, pre] = found;

  process.stderr.write(`compacter: setting up (one time) in ${HOME}\n`);
  fs.mkdirSync(HOME, { recursive: true });
  if (!fs.existsSync(VENV_PY)) {
    const v = run(cmd, [...pre, "-m", "venv", HOME], { stdio: ["ignore", "ignore", "pipe"] });
    if (v.status !== 0) fail(`could not create a virtualenv: ${(v.stderr || "").trim().split("\n").pop()}`);
  }
  let r = pipInstall(VENV_PY, `${PYPI_NAME}==${VERSION}`);
  if (r.status !== 0) {
    // Not on PyPI yet (or offline): fall back to the GitHub source.
    r = pipInstall(VENV_PY, GIT_URL);
  }
  if (r.status !== 0) {
    fail(`could not install ${PYPI_NAME}: ${(r.stderr || "").trim().split("\n").pop()}`);
  }
  fs.writeFileSync(MARKER, VERSION);
}

ensureInstalled();
const res = spawnSync(VENV_PY, ["-m", "compacter", ...process.argv.slice(2)], { stdio: "inherit" });
if (res.error) fail(res.error.message);
process.exit(res.status === null ? 1 : res.status);
