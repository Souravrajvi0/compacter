# compacter

Choose what your coding agent keeps when a long chat gets compacted, and carry context into a new chat.
Works with Claude Code, Codex and opencode.

```bash
npx compacter doctor
npm install -g compacter
```

This npm package is a small launcher. compacter itself is a Python tool (needs **Python 3.10+**).
On first run it installs `compacter-agent` from PyPI into a private virtualenv at `~/.compacter/npm-venv`
(falling back to the GitHub source if PyPI doesn't have this version), then runs it. Your global Python is untouched.

Full docs: https://github.com/Souravrajvi0/compacter
