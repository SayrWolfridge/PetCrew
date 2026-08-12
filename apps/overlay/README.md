# PetCrew overlay

Windows overlay built with React, TypeScript, Vite, and a Tauri 2 shell. It supports both deterministic fixtures and an authenticated local event hub.

## Commands

```powershell
npm install
npm test
npm run build
npm run tauri:dev
npm run tauri:build
```

The web simulator reads only `../../tests/fixtures/demo-team.json`. The native `Local hub` mode binds to a dynamic loopback port, validates authenticated events, and streams normalized snapshots into the same UI. Its bundled ten-agent generator identifies itself as `simulator`, not Codex.

No provider hook is installed and no live Codex or OpenCode state is read or modified.

Rust is required only for `tauri:dev` and `tauri:build`.
