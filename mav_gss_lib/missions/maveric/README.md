# MAVERIC Mission Package

The MAVERIC mission implementation — one pluggable mission package within the
broader ground station platform. The platform loads it by convention at startup
when mission id `maveric` is active and calls `mission.py::build(ctx)` to obtain
a `MissionSpec`.

MAVERIC runs on the platform's declarative XTCE-lite pipeline. `mission.yml` is
the single source of truth for parameter types, parameters, sequence
containers, meta-commands, verifier rules, UI columns, and wire framing.
Mission Python is the wiring layer around that database: codec, command grammar,
calibrators, alarm predicates, file-chunk side effects, and HTTP routers.

## Layout

```
maveric/
├── __init__.py
├── mission.py             MissionSpec assembler (build(ctx) entry point)
├── mission.yml            XTCE-lite mission database (gitignored)
├── mission.example.yml    Public-safe template for mission.yml
├── declarative.py         YAML parser + codec + command ops + framer wiring
├── codec.py               MaverPacketCodec (PacketCodec) — owns node/ptype tables
├── packets.py             DeclarativePacketsAdapter + MaverMissionPayload + mission facts
├── calibrators.py         Calibrator registry (CALIBRATORS) — raw→engineering decoders
├── schema_types.py        Mission-specific declarative schema types
├── tracking_defaults.py   seed_tracking_defaults — MAVERIC TLE / station / freq seeds
├── alarm_predicates.py    Alarm predicate registry (norm checks, eclipse-aware SV)
├── plugin_tx_builder.py   FastAPI route feeding the TX builder frontend plugin
├── errors.py              Declarative-pipeline error types
├── preflight.py           Mission preflight-check factory (mission.yml + libfec)
│
└── files/                 Downlink-file plugin (REST + event source)
    ├── store.py           ChunkFileStore (chunk reassembly, restart recovery)
    ├── adapters.py        FileKindAdapter implementations (image / AII / MAG)
    ├── registry.py        build_file_kind_adapters — wires adapters from mission_cfg
    ├── repair.py          Stream-repair helpers (e.g. JPEG EOI for partial previews)
    ├── router.py          /api/plugins/files FastAPI router
    └── events.py          MavericFileChunkEvents (EventOps source)
```

## What this package owns

- **mission.yml** — parameter types (with calibrators), parameters,
  sequence containers, meta-commands, verifier specs/rules, UI columns, and the
  `framing:` chain. The platform walker decodes telemetry and encodes commands
  directly from this file.
- **`MaverPacketCodec`** (`codec.py`) — owns node + ptype lookup tables, the
  command wire format (`[src][dest][echo][ptype][id_len][args_len][id][args][CRC-16]`),
  and CSP CRC32 trailer logic. Implements the platform `PacketCodec` Protocol.
- **`DeclarativePacketsAdapter`** (`packets.py`) — wraps the codec into platform
  `PacketOps` (normalize → parse → classify → match_verifiers). Owns CSP V1
  4-byte strip, body CRC and CSP CRC32 verification, duplicate fingerprinting,
  uplink-echo detection, and MAVERIC `mission.facts` for RX UI/log filtering.
  Returns `MaverMissionPayload` instances as the internal walker payload.
- **Wire framing** (declared in `mission.yml` under `framing:`) — composed
  by the platform-side `DeclarativeFramer` from a CSP v1 layer + ASM+Golay
  outer framing. No mission-side framer class; the chain is data, not code.
- **Command operator grammar** (`declarative.py`) — wraps the generic
  declarative command ops with MAVERIC CLI forms:
  `CMD [args...]`, `DEST ECHO TYPE CMD [args...]`, and
  `SRC DEST ECHO TYPE CMD [args...]`. Frontend builder payloads use the
  canonical dict shape `{cmd_id, args, packet}`.
- **Calibrators** (`calibrators.py`) — Python implementations of
  parameter-type calibrators referenced by name in `mission.yml`
  (`maveric.bcd_time`, `maveric.adcs_tmp`, `maveric.gnc_planner_mode`, …).
  Exposed as the `CALIBRATORS` dict; passed to `parse_yaml(..., plugins=)`
  (the platform parameter is named `plugins` because it accepts arbitrary
  Python escape-hatch callables — alarm predicates use the same hook).
- **Alarm predicates** (`alarm_predicates.py`) — Python predicates that
  inspect calibrated values and emit `(Severity, message)` for the alarm
  framework. Wired via `MissionSpec.alarm_plugins`.
- **Frontend route** (`plugin_tx_builder.py`) — `/api/plugins/maveric/identity`,
  the read-only feed for `web/src/plugins/maveric/TxBuilder.tsx` (node /
  ptype / gs_node tables for the dropdowns).
- **RX mission facts** (`packets.py`) — structured MAVERIC header/protocol/
  integrity fields under `mission.facts`; the frontend derives rows and detail
  panes from those facts plus platform parameter updates.
- **Files plugin** (`files/`) — source-scoped chunk reassembly through the
  generic `ChunkFileStore`, plus per-kind logic in `FileKindAdapter`s for
  image, AII, and MAG downlinks. `MavericFileChunkEvents` drives the store
  from inbound file-chunk commands and broadcasts `file_progress`. Files are
  keyed by `(source, filename)` so equal spacecraft filenames do not overwrite.
  Chunk state survives restart via `.chunks/` data and `.meta.json` sidecars,
  and partial JPEGs get a safety EOI marker so previews remain viewable during
  transfer. The router mounts at `/api/plugins/files`; on-disk artifacts live
  under `<log_dir>/files/`.
- **Tracking seeds** (`tracking_defaults.py`) — `seed_tracking_defaults`
  populates `platform.tracking.{stations,tle,frequencies,display}` with
  MAVERIC's actual values at mission build time so a fresh checkout works
  without the operator writing a `gss.yml` first.
- **Frontend plugin surface** — TX builder + Files page + GNC page + EPS page
  under `mav_gss_lib/web/src/plugins/maveric/`.

## MAVERIC-specific behavior (not platform-level)

- **CSP v1 + Command Wire Format** — MAVERIC's inner framing layers, wrapped
  by an ASM+Golay outer chain. The full chain is declared in `mission.yml`
  under `framing:` and assembled by the platform `DeclarativeFramer`.
- **CRC-16 per command + CRC-32C over CSP** — dual integrity scheme is
  MAVERIC's wire format, not a platform requirement. Verified in
  `packets.py::parse`.
- **Node / ptype integer IDs** — MAVERIC maps integers (LPPM=1, EPS=2, …) to
  names. The codec is the runtime owner of these tables.
- **Uplink mode** — MAVERIC uses ASM+Golay (Mode 5) exclusively. The
  `framing:` block in `mission.yml` declares the chain. This is the MAVERIC
  package's current operational profile, not a platform limitation: the GSS
  `FRAMERS` registry also supports AX.25 (`ax25`) for missions or test profiles
  that declare an AX.25 chain.
- **Satellite time decoding** — BCD-time and related engineering values are
  emitted as normal declarative `ParamUpdate` rows. The frontend and log viewer
  consume those rows through the shared parameter/rendering path rather than a
  separate telemetry contract.

## HTTP and event surface

`mission.py::build(ctx)` mounts two MAVERIC routers through `HttpOps`:

| Route prefix | Owner | Purpose |
|--------------|-------|---------|
| `/api/plugins/maveric/identity` | `plugin_tx_builder.py` | Read-only node / ptype / GS-node data for `web/src/plugins/maveric/TxBuilder.tsx`. |
| `/api/plugins/files` | `files/router.py` | Status, file listing, chunk listing, preview, delete, and per-client UI state for image / AII / MAG downlinks. |

`MavericFileChunkEvents` is the mission `EventOps` source. It watches decoded
file-chunk packets, drives the `ChunkFileStore` through the active
`FileKindAdapter`s, broadcasts `file_progress`, and replays known progress to
new `/ws/rx` clients.

## Config shape

At runtime MAVERIC's `mission_cfg` carries these operator-editable keys under
`mission.config` in the native split-state shape:

| Key | Source | Operator-editable? |
|-----|--------|---------------------|
| `csp.*`, `imaging.thumb_prefix` | `mission.py::_seed()` placeholders overlaid by `gss.yml:mission.config.*` | Yes — `MissionConfigSpec.editable_paths` |

Identity-shape keys (mission name, nodes, ptypes, …) live in `mission.yml`
extensions. The codec is the runtime protection — there is no separate
`MissionConfigSpec.protected_paths` set.

Mission-declared RX/TX defaults (`rx.frequency`, `tx.frequency`) are seeded on
`platform_cfg` at build time and can be overridden in `gss.yml`.
Mission-declared tracking defaults (station catalog, TLE, frequencies, day/night
map flag) are seeded by `tracking_defaults.seed_tracking_defaults` into
`platform.tracking.*`; operator overrides in `gss.yml` win.
The downlink-file output directory is fixed at `<log_dir>/files/` and is built
from `ctx.data_dir` at mission build time (no separate operator-editable knob).

## Warning: do not copy as-is

The MAVERIC MissionSpec implementation is tailored to MAVERIC's wire format,
node topology, and command schema. New missions should author their own
`mission.yml` plus a small Python wiring layer using
`mav_gss_lib.platform.spec` (declarative walker, codec contract, command-ops
factory). See `mav_gss_lib/missions/echo_v2/` and
`mav_gss_lib/missions/balloon_v2/` for minimal reference implementations.
