# compacter

**You decide what your coding agent remembers.** When a long chat gets compacted, compacter lets you choose which
messages are kept word for word, which are summarized, and which are dropped, and lets you carry context into a
new chat. Works with Claude Code, Codex and opencode. Your original chat is never changed.

```bash
npx compacter doctor              # check your installed agents against the tested versions
npm install -g compacter
```

## Quick start

```bash
cd your-project
compacter start --agent claude-code     # import the current chat
compacter status                        # list messages with their tags
compacter compact --preview             # read the summary before anything is written
compacter compact --apply               # write a new, compacted chat next to the original
compacter carry --agent claude-code --only 3-9 --new    # carry just some messages into a new chat
```

In real Claude Code testing, a 418-message chat (about 59,000 tokens) compacted to about 2,200 tokens and was
resumed with the key facts intact.

## About this package

This npm package is a small launcher. compacter itself is a Python tool and needs **Python 3.10+** on your machine.
On first run the launcher installs it into a private virtualenv at `~/.compacter/npm-venv` (from PyPI, falling back
to the GitHub source), then runs it. Your global Python is untouched. Delete that folder to reset.

Full docs: https://github.com/Souravrajvi0/compacter
