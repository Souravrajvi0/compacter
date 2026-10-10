// compacter /compacter — opencode TUI plugin.
// Adds a `/compacter` slash command: tick the messages to compact (✓ on the right),
// confirm, and the current session is replaced by a compacted one. Same terminal.
// Shells out to the `compacter` CLI (set COMPACTER_BIN to override); keep-recent via COMPACTER_KEEP_RECENT.
import { spawn } from "node:child_process"

const KEEP_RECENT = Number(process.env.COMPACTER_KEEP_RECENT || 6)
const CMD = process.env.COMPACTER_BIN ? [process.env.COMPACTER_BIN] : ["compacter"] // COMPACTER_CMD (installer rewrites this)

function run(args, { input, env } = {}) {
  return new Promise((resolve) => {
    const child = spawn(CMD[0], [...CMD.slice(1), ...args], { env: { ...process.env, ...env }, shell: /\.(cmd|bat)$/i.test(CMD[0]) })
    let out = ""
    child.stdout.on("data", (d) => (out += d))
    child.stderr.on("data", (d) => (out += d))
    child.on("error", (e) => resolve({ code: -1, out: String(e) }))
    child.on("close", (code) => resolve({ code, out }))
    if (input !== undefined) child.stdin.end(input)
  })
}

function describe(api, msg) {
  const parts = api.state.part(msg.id) ?? []
  const bits = []
  for (const p of parts) {
    if (p.type === "text" && p.text?.trim()) bits.push(p.text)
    else if (p.type === "tool") bits.push(`[${p.tool}]`)
  }
  return bits.join(" ").replace(/\s+/g, " ").trim()
}

function candidates(api, sessionID) {
  const rows = []
  for (const m of api.state.session.messages(sessionID) ?? []) {
    if (m.role !== "user" && m.role !== "assistant") continue
    const text = describe(api, m)
    if (text) rows.push({ id: m.id, role: m.role, text })
  }
  return rows.slice(0, Math.max(0, rows.length - KEEP_RECENT)) // recent window stays verbatim
}

function openPicker(api, rows, selected, current) {
  const n = selected.size
  const options = [
    { title: n ? `▶ Compact ${n} selected message${n === 1 ? "" : "s"}` : "▶ Nothing selected", value: "__go", category: "Actions" },
    { title: "Select all", value: "__all", category: "Actions" },
    { title: "Select none", value: "__none", category: "Actions" },
    ...rows.map((r, i) => ({
      title: `${String(i).padStart(3)} ${r.role === "user" ? "you" : "ai "}  ${r.text.slice(0, 90)}`,
      value: r.id,
      footer: selected.has(r.id) ? "✓" : " ", // right-hand checkmark
      category: "Messages",
    })),
  ]
  api.ui.dialog.setSize("large")
  api.ui.dialog.replace(() =>
    api.ui.DialogSelect({
      title: "Compacter — space/enter toggles ✓, pick ▶ to compact",
      options,
      current: current ?? "__go",
      flat: false,
      onSelect: (opt) => {
        const v = opt.value
        if (v === "__go") return n ? compact(api, [...selected]) : api.ui.toast({ variant: "warning", message: "Tick at least one message first" })
        if (v === "__all") rows.forEach((r) => selected.add(r.id))
        else if (v === "__none") selected.clear()
        else selected.has(v) ? selected.delete(v) : selected.add(v)
        openPicker(api, rows, selected, v)
      },
    }),
  )
}

async function compact(api, ids) {
  const dir = api.state.path.directory
  const route = api.route.current
  const sessionID = route.name === "session" ? route.params.sessionID : undefined
  api.ui.dialog.clear()
  api.ui.toast({ variant: "info", message: `Compacting ${ids.length} messages…`, duration: 120000 })
  const env = sessionID ? { COMPACTER_OPENCODE_SESSION: sessionID } : {}
  let r = await run(["--project", dir, "start", "--agent", "opencode"], { env })
  if (r.code === 0) r = await run(["--project", dir, "compact", "--apply", "--keep-recent", String(KEEP_RECENT), "--only-ids", "-"], { input: ids.join("\n"), env })
  const newID = /COMPACTER_NEW_SESSION=(\S+)/.exec(r.out)?.[1]
  if (r.code !== 0 || !newID) {
    api.ui.toast({ variant: "error", title: "Compacter failed", message: r.out.trim().split("\n").slice(-3).join(" | ").slice(0, 300), duration: 15000 })
    return
  }
  api.ui.toast({ variant: "success", message: "Compacted — switched to the new session", duration: 6000 })
  api.route.navigate("session", { sessionID: newID })
}

export default {
  id: "compacter-compacter",
  tui: async (api) => {
    api.command.register(() => [
      {
        title: "Compacter: choose messages to compact",
        value: "compacter.compacter",
        description: "Tick the messages to fold into a summary; the rest stay verbatim",
        category: "Compacter",
        slash: { name: "compacter", aliases: ["compact-pick"] },
        onSelect: () => {
          const route = api.route.current
          if (route.name !== "session") return api.ui.toast({ variant: "warning", message: "Open a session first" })
          const rows = candidates(api, route.params.sessionID)
          if (!rows.length) return api.ui.toast({ variant: "info", message: "Nothing old enough to compact yet" })
          openPicker(api, rows, new Set(rows.map((r) => r.id)))
        },
      },
    ])
  },
}
