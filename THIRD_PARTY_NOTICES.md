# Third-party notices

PetCrew is licensed under MIT. Dependencies, build tools, and external
platforms remain under their respective licenses. This file is a reviewable
source-level inventory; the exact resolved versions are authoritative in
`apps/overlay/package-lock.json` and `apps/overlay/src-tauri/Cargo.lock`.

## Direct JavaScript dependencies and build tools

| Component | Resolved version | License in lockfile |
| --- | ---: | --- |
| `@tauri-apps/api` | 2.11.1 | Apache-2.0 OR MIT |
| `react` | 19.2.7 | MIT |
| `react-dom` | 19.2.7 | MIT |
| `@tauri-apps/cli` | 2.11.4 | Apache-2.0 OR MIT |
| `@types/node` | 26.1.1 | MIT |
| `@types/react` | 19.2.17 | MIT |
| `@types/react-dom` | 19.2.3 | MIT |
| `@vitejs/plugin-react` | 6.0.3 | MIT |
| `esbuild` | 0.28.1 | MIT |
| `typescript` | 7.0.2 | Apache-2.0 |
| `vite` | 8.1.5 | MIT |
| `vitest` | 4.1.10 | MIT |

The current npm lockfile also contains transitive packages declared under MIT,
Apache-2.0, Apache-2.0 OR MIT, MPL-2.0, BSD-3-Clause, ISC, and 0BSD. In
particular, the build graph contains `lightningcss` platform packages under
MPL-2.0. Those packages are not relicensed by PetCrew's MIT license.

## Direct Rust dependencies and build tools

| Component | Resolved version | Upstream license |
| --- | ---: | --- |
| `axum` | 0.8.9 | MIT |
| `chrono` | 0.4.45 | MIT OR Apache-2.0 |
| `futures-util` | 0.3.32 | MIT OR Apache-2.0 |
| `rand` | 0.9.5 | MIT OR Apache-2.0 |
| `rusqlite` | 0.37.0 | MIT |
| `serde` | 1.0.228 | MIT OR Apache-2.0 |
| `serde_json` | 1.0.150 | MIT OR Apache-2.0 |
| `sha2` | 0.10.9 | MIT OR Apache-2.0 |
| `tauri` | 2.11.5 | Apache-2.0 OR MIT |
| `tokio` | 1.53.0 | MIT |
| `tower-http` | 0.7.0 | MIT |
| `url` | 2.5.8 | MIT OR Apache-2.0 |
| `tower` (tests) | 0.5.3 | MIT |
| `tauri-build` | 2.6.3 | Apache-2.0 OR MIT |

The `rusqlite` `bundled` feature builds SQLite. SQLite's upstream source is
dedicated to the public domain; downstream distributors remain responsible for
verifying the exact bundled release and required notices.

## Release boundary

Source publication does not by itself produce a complete binary-distribution
notice bundle. Before publishing an installer or binary release:

1. derive a full dependency report from both lockfiles;
2. include every required license text, copyright notice, and NOTICE file;
3. review file-level copyleft conditions, including MPL-2.0 components;
4. record the report with the exact release commit and artifact checksums.

No third-party software or asset is covered merely because it interoperates
with PetCrew.
