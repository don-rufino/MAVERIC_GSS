# AGENTS.md

Maintainer and Codex reference for this repository. Local-only — do not commit.

## Project overview

MAVERIC GSS (Ground Station Software) is the ground station suite for the MAVERIC CubeSat mission. The software and SDR stack (USRP B210 + GNU Radio `MAV_DUPLEX` flowgraph) are full-duplex capable; the deployed station runs half-duplex over a single UHF antenna with a coax switch. The operator surface is a web dashboard served by `MAV_WEB.py` (FastAPI backend + React SPA). Legacy Textual TUIs (`MAV_RX.py`, `MAV_TX.py`) have been retired.

## Current addendum

These notes capture newer implementation details without rewriting the older reference sections below.

- Current tracked GNU Radio assets are `gnuradio/MAV_DUO.py`, `gnuradio/MAV_DUO.grc`, `gnuradio/MAVERIC_DECODER.yml`, plus the Astrocast pair `gnuradio/MAV_ASTROCAST.py` (Qt GUI mirroring MAV_DUO conventions, `--headless` opt-out; hand-written — deliberately no `.grc`) and `gnuradio/ASTROCAST_DECODER.yml`. Treat older `MAV_DUPLEX` wording as legacy naming.
- Mission switching: the operator config is per-mission — `_active_gss_path()` in `config.py` maps `GSS_MISSION` env (set by `MAV_WEB.py --mission <id>` or a switch-restart) to `gss.<id>.yml`; maveric keeps legacy `gss.yml`. `GSS_MISSION` also force-overrides `mission.id` at load. `POST /api/mission/switch` (`server/api/mission.py`, token-guarded) replies, then a background thread runs `tracking.disengage()` + `radio.shutdown()` (execv never runs lifespan shutdown — skipping this orphans the old flowgraph on the SDR) and `_reexec`s with argv rewritten to `--mission <id>` + `MAV_MISSION_SWITCHED` env (suppresses the duplicate browser tab; do NOT reuse `MAV_UPDATE_APPLIED` — it renders an "Updated to sha" row). `GET /api/missions` lists deployable missions via `platform/loader.py::discover_missions()` (= packages shipping a mission.yml, so fixtures never appear). Frontend: Mission pane select in `ConfigModal` → `ConfirmDialog` → switch POST → overlay + `lib/restart.ts::waitForMissionThenReload` (polls `/api/status` until `.mission == target` — poll-until-200 alone would race the old pre-exec process). Plain `python3 MAV_WEB.py` always boots maveric (non-sticky by design): on real launches the active mission is `GSS_MISSION` or maveric — `gss.yml`'s `mission.id` does NOT select a non-default mission (config.py `load_split_config`, `explicit_path` branch is tools/tests only). This is deliberate: it keeps `gss.yml` MAVERIC-only so a non-default mission can never run out of it and overwrite MAVERIC's csp routing on the next save. `radio.script` is NOT a platform default (RadioService's `DEFAULT_RADIO_SCRIPT` covers maveric); each mission seeds its own flowgraph so a mission can actually override it.
- `missions/astrocast/` is a real RX-only third-party mission (Astrocast 0.1, NORAD 43798, 437.150 MHz): `commands=None` (platform rejects TX admission cleanly), tracked `mission.yml` (only maveric's is gitignored), ascii_tokens `beacon_hk` container fed by a `_TokenPacket` walker packet built in `packets.py`, `mission_name`/rx-frequency/tracking seeded in `build(ctx)`. Beacon = non-standard FX.25 (AX.25 UI in RS(255,223)); `$HK` clock = 48-bit hex ticks of 1/65536 s since 2016-01-01. 9k6 CCSDS-RS downloads are opaque (logged raw, `TLM` facts, no parameters). Any mission booted through `create_app` MUST ship a `spec_root` — `build_alarm_environment` dereferences it unguarded at server startup (`server/app.py:101`).
- The GSS framing toolkit supports more than the MAVERIC default: `FRAMERS` includes `csp_v1`, `ax25`, and `asm_golay`. Root README should surface AX.25 / HDLC / G3RUH support as a platform feature while still naming MAVERIC's configured chain as CSP v1 -> ASM+Golay.
- `platform.radio` is now part of split platform config. Defaults: `enabled: true`, `autostart: false`, `script: gnuradio/MAV_DUO.py`, `log_lines: 1000`. `DEFAULT_PLATFORM_CONFIG_SPEC` allows the `radio` and `tracking` sections through `/api/config` (in addition to `tx` and `rx`).
- `RadioService` lives in `mav_gss_lib/server/radio/service.py`. It supervises one optional GNU Radio child process, captures stdout/stderr, exposes `/api/radio/status`, `/api/radio/logs`, `/api/radio/start`, `/api/radio/stop`, `/api/radio/restart`, and broadcasts over `/ws/radio`.
- `platform.tracking` is a first-class platform subsystem (depends on `skyfield`, `numpy`). Platform `_DEFAULTS["tracking"]` seeds only the doppler control block (`control.{rx_zmq_addr,tx_zmq_addr,tick_period_s}` on `tcp://127.0.0.1:52003` / `52004`). The active mission's `build(ctx)` seeds the station catalog, TLE block (`tle.{source,name,line1,line2}`), `frequencies.{rx_hz,tx_hz}`, and `display.day_night_map`; for MAVERIC that lives in `missions/maveric/tracking_defaults.py::seed_tracking_defaults`. Tracking math lives in `mav_gss_lib/platform/tracking/` (`config.py`, `models.py`, `propagation.py`). `TrackingService` (`mav_gss_lib/server/tracking/service.py`) reads live config per call and selects a `DopplerSink` implementation: `ZmqDopplerSink` (real, backed by `server/tracking/sink_zmq.py`) when engaged, `NullDopplerSink` otherwise. The 1 Hz tick loop in `server/tracking/_tick.py` drives `DopplerBroadcaster` over `/ws/tracking`. REST endpoints: `/api/tracking/config`, `/api/tracking/state`, `/api/tracking/passes`, `/api/tracking/pass/{id}`, `/api/tracking/doppler`, and `POST /api/tracking/doppler/connection/{connect|disconnect}`.
- The React shell has only two built-in tabs: Dashboard (`__dashboard__`) and Radio (`__radio__`); everything else is a mission plugin. Tracking is **not** a separate tab — `DopplerSection` renders inside the Radio tab (`components/radio/RadioPage.tsx`) via `useTrackingSocket` (`components/radio/useTrackingSocket.ts`). App-wide tracking state lives in `state/TrackingProvider.tsx`. There is no `web/src/components/tracking/` page, no `vite.tracking-preview.config.ts`, and no `dev:tracking-preview` / `build:tracking-preview` npm scripts; the `dist-tracking-preview/` directory is a stale leftover and is gitignored. `App.tsx` does not implement any `?panel=tx` / `?panel=rx` pop-out mode. `ConfigModal` (`web/src/components/config/ConfigModal.tsx`) is a centered two-pane modal — a left category rail (Mission / Radio·RF / Tracking / About) plus a content pane, opened by the header gear. It takes only `open` / `onClose`, has no `initialSection` prop, and filters fields with a client-side `matchesQuery` search. The Tracking pane has a CelesTrak/Space-Track provider dropdown (`tle_fetch.provider`); when Space-Track is selected it shows read-only env-presence of `SPACETRACK_IDENTITY`/`SPACETRACK_PASSWORD` from `GET /api/tracking/tle/status` (booleans only — creds stay env-only, never entered in the app).
- RX is split into explicit stages: `RxIngestRecord` in `platform/rx/records.py`, `DropOldestQueue` in `server/rx/queueing.py`, accepted-RX binary journal in `server/rx/journal.py`, decoded packet replay/detail cache in `server/rx/detail_store.py`, and side-effect projection in `server/rx/projections.py`. Packet details are available at `/api/rx/packets/{event_id}`.
- `/ws/rx` can send either a single `rx_packet` or an `rx_batch`. Browser state is buffered in `web/src/hooks/rxSocketState.ts` and flushed by `useRxSocket` about every 50 ms.
- TX queue item types are now `mission_cmd`, `delay`, `note`, and `checkpoint`. Checkpoints pause the send loop with `guard_confirm.kind = "checkpoint"` until the operator approves. Send-loop orchestration lives in `server/tx/_send_coordinator.py`.
- TX command verification uses a shared `event_id`: the same id is written to the `tx_command` log record, `CommandInstance.cmd_event_id`, queue/history websocket payloads, and frontend verifier maps.
- MAVERIC's downlink-file plugin is `missions/maveric/files/` (the older `imaging/` subdir was generalized away). `ChunkFileStore` (`files/store.py`) keys products by `(source, filename)` and stores chunks under source directories; per-kind logic (image / AII / MAG) lives in `FileKindAdapter` impls (`files/adapters.py`). Pairing of full/thumbnail files keys off `mission.config.imaging.thumb_prefix` (legacy config name retained). `MavericFileChunkEvents` (`files/events.py`) drives the assembler from inbound packets and broadcasts `file_progress` messages. The router mounts at `/api/plugins/files` (`files/router.py`); on-disk artifacts live under `<log_dir>/files/`.
- Strict JSON responses should pass values through `platform/json_safety.py::json_safe` or equivalent non-finite float stripping before reaching `JSONResponse`.
- MAV_DUO waterfall autosave: `_WaterfallLogger` (in `MAV_DUO.py`, mirrored as `epy_block_waterfall` in the `.grc`) logs 1024-bin FFT rows from the 200 ksps RX stream to `<log_dir>/waterfalls/waterfall_<ts>.dat` (~10 rows/s) and renders a SatNOGS-style PNG via `gnuradio/waterfall_render.py` on flowgraph stop; crash leftovers render on next start. `RadioService._frequency_env` injects `GSS_WATERFALL_DIR` (absolute). MAV_ASTROCAST intentionally not covered.

## Running

Requires the **radioconda** environment with GNU Radio 3.10+, gr-satellites, PyZMQ, pmt, PyYAML, crcmod, pydantic v2, skyfield, numpy, uvicorn, fastapi, websockets, and Pillow.

```bash
conda activate                # radioconda base env (auto-activated in most setups)
python3 MAV_WEB.py            # Web dashboard on http://127.0.0.1:8080
python3 MAV_WEB.py --ephemeral  # Redirect every disk-write path to a tempdir (or GSS_EPHEMERAL=1; see server/ephemeral.py)
```

The web UI auto-opens in the default browser. After all browser tabs disconnect, the server exits (`SHUTDOWN_DELAY = 2` seconds, `mav_gss_lib/server/shutdown.py`). Host/port live in `mav_gss_lib/server/state.py` (`HOST = "127.0.0.1"`, `PORT = 8080`).

### Web UI development

UI source is under `mav_gss_lib/web/src/`. The production build at `mav_gss_lib/web/dist/` is committed so the server can serve it without a build step. `node_modules/` stays untracked.

```bash
cd mav_gss_lib/web
npm install
npm run dev                   # Vite dev server with HMR, proxies API to :8080
npm run build                 # Production build to dist/ — commit alongside source
```

Run `npm run build` after every UI source change and commit the updated `dist/` with the source. The build SHA is resolved at server launch (`config._read_build_sha()`) and served via `/api/config` as `general.build_sha`.

## Architecture

### Entry point

`MAV_WEB.py` bootstraps dependencies via `mav_gss_lib.updater.bootstrap_dependencies()`, then creates the FastAPI app from `mav_gss_lib.server.app.create_app()`. The app bridges ZMQ to WebSocket: a background RX thread parses inbound PDUs and fans out to `/ws/rx`; TX commands arrive on `/ws/tx`, get validated, framed, and published via ZMQ PUB. TX queue items are `mission_cmd`, `delay`, or `note`; persisted queue state lives at `.pending_queue.jsonl` under `runtime.log_dir`.

### Mission-decoupled architecture

The platform separates reusable mechanics from mission-specific semantics. Mission packages live under `mav_gss_lib/missions/<name>/`. The active mission is loaded from `mission.id` in the split-state config (default: `maveric`). Fixture missions `echo_v2` and `balloon_v2` prove the boundary.

**Platform owns:** transport, queue, send loop, blackout timing, history, logging envelope, mission loading, generic UI containers/renderers, framing primitives, declarative XTCE-lite spec runtime, parameter cache, verifier registry, alarm engine.

**Mission owns:** packet normalize/parse/classify/verifier matching, command parse/validate/encode/frame/correlation/schema, mission facts and parameter emission, mission event sources, mission HTTP routers, mission-specific preflight, alarm predicate plugins, and the wire framing declaration.

Key contracts in `mav_gss_lib/platform/contract/`:
- **`MissionSpec`** (`contract/mission.py`) — single mission/platform boundary. Bundles `PacketOps` and `MissionConfigSpec` (required) plus optional `CommandOps`, `spec_root` (declarative `Mission`), `spec_plugins`, `events: EventOps`, `http: HttpOps`, `alarm_plugins`, `preflight`, and `parse_warnings`.
- **`PacketOps.normalize / parse / classify / match_verifiers`** — mission converts raw ZMQ bytes into `NormalizedPacket` -> `MissionPacket` -> `PacketFlags`; platform assembles `PacketEnvelope` and asks mission-private logic to match inbound packets to open verifier instances.
- **`CommandOps.parse_input / validate / encode / frame / correlation_key / schema`** — mission-owned command pipeline. `encode` returns `EncodedCommand(raw=..., mission_facts=..., parameters=...)`; `frame` returns `FramedCommand(wire=..., frame_label=..., max_payload=..., log_fields=..., log_text=...)`.
- **Declarative UI columns** — `mission.yml::ui.rx_columns` and `ui.tx_columns` are parsed into `platform.spec.ui.UiSpec` and served by `/api/rx-columns` / `/api/tx-columns`. Frontend renderers consume `mission.facts` and `parameters`; there is no backend `UiOps` contract.
- **`HttpOps`** — mission-owned FastAPI routers auto-mounted by `server/app.py` at startup.
- **`MissionConfigSpec`** — declares `editable_paths` (supports `.*` subtree suffix) and `protected_paths`. `/api/config` mission updates flow through `apply_mission_config_update` against this spec.
- **`MissionContext`** — mission `build(ctx)` receives `platform_config`, `mission_config` (live references to `runtime.platform_cfg` / `runtime.mission_cfg`), and `data_dir`. Captured references stay live across `/api/config` mutations — no MissionSpec rebuild needed.
- **Mission builder registry** (`mav_gss_lib/web/src/plugins/registry.ts`) — convention-based lazy loading of mission-specific TX builder components and plugin pages.

### Server runtime

`WebRuntime` carries split-state config as primary (`platform_cfg`, `mission_id`, `mission_cfg`). Read sites use typed accessors (`runtime.log_dir`, `runtime.mission_name`, `runtime.frame_label`, `runtime.tx_delay_ms`, `runtime.tx_blackout_ms`, `runtime.tx_frequency`, `runtime.parameter_cache`) or `runtime.mission_cfg` / `runtime.platform_cfg` directly. `runtime.frame_label` reads from `mission.spec_root.framing.uplink_label`. `PlatformRuntime.from_split(...)` (with optional `on_parameter_apply` hook) is the primary platform runtime constructor.

Guardrail tests enforce: no `runtime.cfg.get(...)` reads in backend, no `rebuild_flat_cfg()` method, no `mav_gss_lib.platform.framing.*` imports under `server/` (framing is mission-composed; server publishes `FramedCommand.wire` as-is), no `runtime.csp` / `runtime.ax25` references.

### Color system (`src/lib/colors.ts`)

Blacked-out mission console theme with semantic accent colors:

| Tone | Color | Usage |
|---|---|---|
| `danger` | `#FF3838` red | Failure, timeout, invalid, ERR/FAIL/NACK |
| `warning` | `#E8B83A` yellow | Caution, guarded, hazardous |
| `info` | `#5AA8F0` blue | ACK, advisory, sending state |
| `success` | `#3CC98E` green | Confirmed nominal, RES, receiving |
| `active` | `#30C8E0` cyan | Selection, live context, TLM |
| `neutral` | `#888888` gray | CMD, REQ, FILE, disabled, unknown |

Surfaces are pure black (`#080808` / `#0E0E0E` / `#151515`). Borders are `#222222` / `#333333`. Color only appears in small accents: badges, rails, dots, icons. No colored fills on panels, tables, or large surfaces.

### NASA HFDS design targets

- Target minimum operator text size: 11px (HFDS 9.4.1). Current compact UI has a few 9-10px labels/badges in log/imaging/GNC surfaces; do not call the UI fully HFDS-compliant until those are removed or justified.
- Contrast target: >=3:1 minimum (HFDS 9.4.6)
- Flash-rate target by severity: advisory 3 Hz, caution 4 Hz, danger 5 Hz (HFDS 9.8.3)
- Color redundancy target: every color-coded status should also have text/icon/shape (HFDS 9.3.6)
- Core semantic palette target: 6 status tones (`danger`, `warning`, `info`, `success`, `active`, `neutral`) plus sparse frame/debug accents
- Desaturated colors on dark background (HFDS 9.7.3)

### Fonts

- **Inter** — UI text (headers, labels, buttons, column headers)
- **JetBrains Mono** — data rows (packet list, detail values, sent history, hex dumps)

### Protocol stack (MAVERIC)

Framing primitives live in `mav_gss_lib/platform/framing/` — `Framer` Protocol + `FramerChain` composer, plus `csp_v1.py`, `ax25.py`, `asm_golay.py`, `crc.py`. The `__init__.py` ships a `FRAMERS` registry (`csp_v1`, `ax25`, `asm_golay`) and a `build_chain` composer. Operator-key aliasing for CSP (`priority`/`source`/`destination`/`dest_port`/`src_port` ↔ `prio`/`src`/`dest`/`dport`/`sport`) lives at the registry boundary. **Server code does not import these directly** — framing is mission-composed and the server only publishes `FramedCommand.wire` on ZMQ.

MAVERIC declares its chain in `mission.yml::framing:` (`csp_v1` → `asm_golay`); the platform `DeclarativeFramer` reads that block and the live `mission_cfg` per send so `/api/config` edits to `csp.*` propagate without a `MissionSpec` rebuild.

Outward layering: **Command Wire Format -> CSP v1 -> ASM+Golay**.

Command wire format: `[src][dest][echo][ptype][id_len][args_len][id][args][CRC-16]`.

Schema argument types: `str`, `int`, `float`, `epoch_ms`, `bool`, `blob`.

**Uplink framing — ASM+Golay (AX100 Mode 5)** — declared in `mission.yml::framing:`, not operator-selectable. The frame is fixed 312 bytes: 50-byte preamble, 4-byte ASM sync, 3-byte Golay(24,12) length field, and a 255-byte data field containing RS(255,223) + CCSDS-scrambled payload padded with zeroes. NRZ encoding, MSB first. MAVERIC enforces the 223-byte inner CSP cap — TX queue admission calls `mission.commands.frame(encoded)` which raises `ValueError` when oversize.

### Radio hardware

GomSpace NanoCom AX100 transceiver, UHF 430–440 MHz. The code-side MAVERIC uplink path assumes AX100 Mode 5 ASM+Golay framing with CSP over the inner payload. RF baud/modulation and on-orbit radio parameters are configured outside the web app; `platform.framing.asm_golay` documents the expected Mode 5 `csp_rs` / `csp_rand` assumptions.

## Configuration

Config is stored on disk as native split shape `{platform: {...}, mission: {id, config: {...}}}`. Legacy flat files on disk are auto-canonicalized internally by `config._canonical_operator_config()`. `config.load_split_config()` returns `(platform_cfg, mission_id, mission_cfg)` — the primary runtime state held by `WebRuntime`. There is no flat runtime projection.

- **`gss.yml`** — Operator config. Native shape: `platform.tx`, `platform.rx`, `platform.radio`, `platform.tracking`, `platform.general.log_dir`, `platform.stations`, `mission.id`, `mission.config.*`. User values deep-merged over `_DEFAULTS` (platform-only) in `config.py`. Mission-specific defaults seeded by the active mission's `build(ctx)` at startup. Identity keys (nodes, ptypes, node descriptions, GS node) live in `mission.yml` extensions; mission name, command catalog, framing, and UI columns live in top-level `mission.yml` sections, not in `gss.yml`.
- **`mav_gss_lib/missions/maveric/mission.yml`** — XTCE-lite mission database: `parameter_types`, `parameters`, `bitfield_types`, `sequence_containers`, `meta_commands`, `verifier_specs` / `verifier_rules`, `framing:`, and `ui:`. **Gitignored.**
- **`mav_gss_lib/missions/maveric/mission.example.yml`** — Tracked, public-safe template. Copy to `mission.yml` on first run.

### Config ownership

**Platform-owned** (`runtime.platform_cfg`):
- `tx.zmq_addr` (default `tcp://127.0.0.1:52002`), `tx.delay_ms`, `tx.frequency` (operator-overridable; mission-declared default seeded at build time)
- `rx.zmq_addr` (default `tcp://127.0.0.1:52001`), `rx.tx_blackout_ms`
- `radio.enabled`, `radio.autostart`, `radio.script`, `radio.log_lines` — supervised GNU Radio child process.
- `tracking.enabled`, `tracking.selected_station_id`, `tracking.stations[]` (id/name/lat/lon/alt/min_elevation), `tracking.tle.{source,name,line1,line2,method,fetched_at_ms}` (`method`/`fetched_at_ms` are provenance for fetched TLEs), `tracking.tle_fetch.{identifier,auto_refresh,refresh_interval_hours}`, `tracking.frequencies.{rx_hz,tx_hz}`, `tracking.display.day_night_map`.
  - Space-Track creds are env-only (`SPACETRACK_IDENTITY`/`SPACETRACK_PASSWORD`); never stored in config. TLE fetch is the only outbound-HTTP path (`platform/tracking/fetch.py`), guard-tested; auto-refresh OFF by default; manual TLEs (`tle.method=="manual"`) are never auto-overwritten.
- `general.log_dir`, `general.generated_commands_dir`
- `general.version` — semver, single-sourced from `mav_gss_lib/web/package.json`. Runtime-derived; stripped on persist.
- `general.build_sha` — git SHA, resolved at server launch. Runtime-derived; stripped on persist.
- `stations` — station catalog keyed by hostname (install-time; `/api/config` never writes it). Distinct from `tracking.stations[]`, which is the operator-editable pass-prediction catalog.
- `tx.uplink_mode` — legacy key. Stripped on save by `config.py`; not a runtime knob.

**Mission-owned** (`runtime.mission_cfg`):
- For MAVERIC: `csp.*`, `imaging.thumb_prefix` — declared editable in `MissionConfigSpec.editable_paths`.
- Mission identity (nodes, ptypes, node descriptions, GS node) lives in `mission.yml` extensions; mission name, command catalog, framing chain, and UI columns live in top-level mission database sections. Codec/spec parsing are the runtime protection — MAVERIC's `MissionConfigSpec.protected_paths` is empty.

**/api/config routing** (handled by `server/api/config.py`):
- Incoming update is split into a platform bucket (tx, rx, platform-general subset) and a mission bucket.
- Platform: `apply_platform_config_update(...)` — whitelisted deep-merge.
- Mission: `apply_mission_config_update(...)` — spec-driven; mission-declared protections and editable subtrees determine what applies.
- `runtime.mission_cfg` is updated in place so `MissionContext`-captured references stay live. Do not `clear()+update()`; TX framing may read the dict concurrently.
- Persisted as native split via `split_to_persistable()`.

## Logging schema

On-disk JSONL files under `<log_dir>/json/` use one unified session stream for RX, TX, parameters, verifier events, and alarms. File names are `session_<ts>[_<station>][_<op>][_<tag>].jsonl`; there are no separate downlink/uplink files and no text log files in the current writer.

**Every record carries the envelope**:
```
event_id    str   unique per line (uuid4 hex); SQL primary key
event_kind  str   "rx_packet" | "tx_command" | "parameter" | "cmd_verifier" | "alarm" | "radio" | "tracking"
session_id  str   filename stem (matches the file on disk)
ts_ms       int   wall-clock UTC milliseconds
ts_iso      str   ISO 8601 with offset, ms precision
seq         int   packet seq (rx) or command seq (tx); parameter inherits parent seq
v           str   GSS version
mission_id  str   mission.id (e.g. "maveric")
operator    str
station     str
```

**rx_packet**: `frame_label`, `transport_meta`, `inner_hex` + `inner_len`, `duplicate`, `uplink_echo`, `unknown`, `warnings`, `mission` dict. RX has only the inner CSP frame on the wire (outer ASM+Golay framing is stripped by the receiver), so there is no `wire_hex`/`wire_len` distinct from `inner_hex`/`inner_len`. No `_rendering` on disk — the log viewer derives display from generic mission facts.

**parameter** (one per `ParamUpdate` from the declarative walker): envelope + `rx_event_id` (parent rx_packet), `name` (fully qualified `"<domain>.<key>"`, e.g. `"spacecraft.callsign"`), `value`, `unit`, `display_only`. Log viewer splits `name` on the first `.` for display.

**tx_command**: envelope + `frame_label`, `inner_hex` + `inner_len`, `wire_hex` + `wire_len`, `warnings`, and `mission` dict. Command id is canonical at `mission.cmd_id`. Protocol/header facts live under `mission.facts`; the CSP block lives at `mission.facts.protocol.csp_header` (symmetric with rx_packet). `mission.facts.protocol.inner_len` is the inner CSP wire length. Top-level legacy `cmd_id`, `dest`, `src`, `echo`, `ptype`, and `uplink_mode` must not be emitted by the live writer.

**cmd_verifier**: envelope + `cmd_event_id`, `instance_id`, `stage`, `verifier_id`, `outcome`, `elapsed_ms`, and optional `match_event_id`. A closing record (one per instance, written by `_verifier_sweep_loop` when `is_settled(inst)` flips True) carries `verifier_id=""`, `seq=0`, and `outcome` derived from the instance stage (`complete→passed`, `failed→failed`, `timed_out→window_expired`). `is_settled` = terminal stage AND every declared verifier outcome non-pending; admission stays REJECTED for `(cmd_id, dest)` until the instance settles.

**alarm**: envelope + `alarm` object from `SessionLog.write_alarm`. Audit records are transition-only (state or severity change, removal, or operator action); WS broadcast still streams every change so the UI keeps live age updates.

**radio**: envelope + `radio` object capturing GNU Radio supervisor lifecycle (start / stop / crashed) — `action`, `state`, `pid`, `exit_code`, `command`, `script`, `cwd`, `detail`, `expected`. `seq` is 0 by convention.

**tracking**: envelope + `tracking` object capturing operator-initiated Doppler engagements — `action` (connect / disconnect), `mode`, `prev_mode`, `station_id`, `rx_zmq_addr`, `tx_zmq_addr`, `detail`. `seq` is 0 by convention.

**seq=0 convention** — `radio`, `tracking`, and the closing `cmd_verifier` record (the one written when an instance settles) all use `seq=0` because they are out-of-band w.r.t. the RX seq stream. Downstream consumers (SQL ingest, replay, log diff) must key on `event_kind` AND `seq` together — never on `seq` alone — to avoid conflating these distinct event streams.

Envelope stability is guarded by `tests/test_log_schema.py`; API contract by `tests/test_api_logs.py`.

**Migrating legacy logs**: `scripts/migrate_logs.py <log_dir> [--mission-id maveric]` walks `<log_dir>/json/*.jsonl` (old shape) and writes the unified shape to `<log_dir>/json.v2/`. Review, then swap.

## Alarm framework

A unified alarm engine spans platform health, container freshness, and parameter rules. All alarms flow through one 4-state machine (`OK / TRIGGERED / ACKED / LATCHED`) and one WebSocket stream.

Platform package `mav_gss_lib/platform/alarms/` provides the registry (state machine + persistence + latching), schema parser, dispatch fan-out, and three evaluator families: `platform.py` (silence / zmq / crc / dup), `container.py` (freshness, parameter-domain-aware carrier index), `parameter.py` (per-parameter rule evaluation).

Server wiring (`server/app.py` lifespan): build alarm environment → construct `AlarmDispatch` (with `WebRuntimeBroadcastTarget` + audit sink) → bind to runtime → spawn `_alarm_tick_loop` for container + platform evaluators. Parameter alarms run inline on every `ParameterCache` write via the `on_parameter_apply` hook.

Surfaces: `/ws/alarms` (snapshot + change stream + ack + `removed` flag), `/api/parameters` (flat snapshot + freshness block + `parameters_freshness` WS messages), `SessionLog.write_alarm` (JSONL audit log).

## Testing

```bash
conda activate
python3 -m unittest discover -s tests -t . -v             # run everything
python3 -m unittest tests.test_platform_framing_spec      # framing primitives + chain composition
python3 -m unittest tests.test_declarative_framer         # DeclarativeFramer wire output
python3 -m unittest tests.test_platform_architecture      # platform boundary guardrails
python3 -m unittest tests.test_mission_owned_framing      # mission CommandOps.frame contract
python3 -m unittest tests.test_alarm_e2e_soak             # alarm fire/ack/recurrence + carrier suppression
python3 -m unittest tests.test_alarm_registry             # 4-state machine + persistence + latching
python3 -m unittest tests.test_ws_alarms                  # /ws/alarms snapshot + change + ack
python3 -m unittest tests.test_api_parameters             # /api/parameters freshness + parameter cache
python3 -m unittest tests.test_ops_server                 # server wiring
python3 -m unittest tests.test_spec_walker_packet         # declarative walker
python3 -m unittest tests.test_spec_command_ops           # declarative command ops
python3 -m unittest tests.test_spec_verifiers             # verifier rules + dispatch
```

Coverage spans config roundtrips, mission loading, declarative YAML parsing, parameter/bitfield/container decoding, command encoding, verifier derivation and persistence, CRC/CSP/AX.25/ASM+Golay framing, RX/TX pipelines, queue admission, session logging, log migration, alarms, parameter freshness APIs, WebSocket endpoints, preflight, updater, and security/auth guards.

Some tests skip when local-only `mav_gss_lib/missions/maveric/mission.yml` is absent. Full GNU Radio flowgraph coverage is gated behind `MAVERIC_FULL_GR=1` in `tests/ops_test_support.py`.

## Versioning

`mav_gss_lib/web/package.json` is the single source of truth. `config.py::_read_version()` reads it at import time and exposes it via `_DEFAULTS["general"]["version"]`. The `/api/config` save path strips any client-supplied `general.version`.

- Semver `x.y.z`.
- Git tags use the `vX.Y.Z` form (full semver).
- Do not put version numbers in commit messages.
- During in-flight feature work, do not bump version per commit — hold the bump until the feature is verified.
- Each bump: update `package.json`, run `npm run build`, commit both in the release commit.

## General rules

- When a specific file (CSV, JSONL, PDF) is named as the source of truth, read that file directly — do not infer or reconstruct from code analysis.
- Do not push back on user requests. If the user asks for a specific approach or exclusion, follow it directly; do not suggest alternatives unless asked.
- Do not commit `AGENTS.md`; it is local-only. Keep `mav_gss_lib/missions/maveric/mission.yml` gitignored.
- Skip defensive input validation on operator-facing config paths. Trained operators will not enter invalid values.
- Remove `.playwright-mcp/` artifacts before every commit — physical deletion, not just a gitignore entry.
- Do not commit plan/spec documents (e.g., files under `plans/`, `specs/`, or `docs/`, including HTML UI previews). They stay local. The repo's only tracked written reference is this `AGENTS.md` plus the mission/web README files.

## UI / styling

- Blacked-out mission console theme. Color is sparse — only badges, rails, dots, icons, border accents.
- Do not add colored fills to panels, tables, or large surfaces.
- When editing UI styling, make minimal targeted changes. Do not restyle adjacent elements.
- If a visual detail is ambiguous (color choice, pill vs. text, icon selection), ask before implementing.
- Follow the HFDS targets above for new UI work: avoid adding new sub-11px text, keep contrast >=3:1, use color redundancy, and use 3 / 4 / 5 Hz flash rates by severity.
- Prefer descriptive, structured terminology — rename code rather than adding glossary comments.
- GNC dashboard scope is attitude / navigation / control only. Do not add pass tracking, AOS/LOS, or link/RSSI widgets there.

## Code quality

After any refactoring or multi-file edit, verify imports and module-qualified references. Run tests where available. Build the web UI (`npm run build` in `mav_gss_lib/web`) to catch TypeScript errors. If UI source changed, commit the updated `dist/` alongside the source. Prefer code fixes (extract a helper, add a type, split a function) over documentation or comments. Explain proposed changes before applying large edits.
