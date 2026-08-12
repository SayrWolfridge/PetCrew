# OpenCode adapter

`petcrew.js` is a dependency-free global local OpenCode plugin candidate. It maps only supported,
sanitized OpenCode lifecycle events to `docs/EVENT_PROTOCOL.md` and uses the existing authenticated
PetCrew loopback hub plus latest-state registry.

The file in this repository is inert. Do not copy it into
`%USERPROFILE%\.config\opencode\plugins\` or restart OpenCode without the explicit activation
gate in `docs/OPENCODE_ADAPTER_AUDIT.md`.

Run its isolated tests with:

```powershell
node --test adapters/opencode/petcrew.test.mjs
```

The adapter deliberately does not read OpenCode messages, tool inputs/results, commands, question
text/options, permission patterns/answers, credentials, or the local database. Its only full-path
field is the current session directory used for the explicit `Открыть проект в OpenCode`
navigation target; the display project path remains null.

The bounded startup pass also repairs a missed CLI completion only when the adapter's own latest
registry record is a fresh matching root `working` event and the official session status is idle or
absent. Historical idle sessions, attention states, terminal records, children, stale records, and
malformed identities remain silent.
