# Local build artifacts

`PetCrew-simulator.exe` is the verified Windows simulator and authenticated-local-hub build. It is intentionally ignored by Git and rebuilt from source.

Verification completed on 2026-07-18:

- size: 3,718,144 bytes;
- eight TypeScript tests and seven Rust core tests: passed;
- native build and hub smoke test: passed;
- loopback authentication, replay rejection, payload limits, CORS, minimal cache, and runtime cleanup: passed;
- titlebar and pet-area dragging: manually verified by the user;
- explicit close button: manually verified by the user;
- ten-agent hub loading and live step changes: manually verified by the user;
- window title: `PetCrew`;
- SHA-256: `3FA202BB4EBDB93659D1A16AE0FFC7D7C64540A6D829AA005051BC37347D1B56`.

The build supports the bundled fixture and an authenticated local simulator that exercises the real hub. It does not install hooks or read live Codex/OpenCode state.
