# Adding a mission package

Every subdirectory here is a mission package: a Python package that the
platform loads by id and asks to build a `MissionSpec` — the single
mission/platform boundary (`mav_gss_lib/platform/contract/mission.py`).
The platform owns transport, queueing, logging, tracking, alarms, and the
web shell; the mission owns packet parsing, telemetry semantics, command
encoding, framing, and its own defaults. Adding a satellite means adding
a folder here — never editing `mav_gss_lib/platform/`.

## Package anatomy

```
mav_gss_lib/missions/<id>/
  __init__.py            required — one-line docstring is enough
  mission.py             required — exports build(ctx) -> MissionSpec
  mission.yml            declarative mission database; its presence makes
                         the mission deployable (see below)
  packets.py             PacketOps implementation (or telemetry.py for
                         missions that split decode tables out)
  tracking_defaults.py   optional — seeds TLE / frequencies / station
  README.md              recommended — protocol notes, enabling steps
```

The mission id **is** the folder name. It must be a valid lowercase Python
identifier — the loader does
`importlib.import_module("mav_gss_lib.missions.<id>.mission")`
(`platform/loader.py::load_mission_spec_from_split`).

**`mission.yml` presence = deployable.** `discover_missions()` lists every
package that ships a `mission.yml`; that list backs `GET /api/missions` and
the in-app mission switcher. Packages without one never surface there —
that is how test fixtures (`echo_v2`, `balloon_v2`) and shared libraries
(`ax100_rx`, used by the AX100 family) stay out of the operator UI.

## Step by step

### 1. Create the package

```bash
mkdir mav_gss_lib/missions/<id>
```

Give `__init__.py` a one-line docstring saying what the mission is.

### 2. Implement `PacketOps` (`packets.py`)

The contract is four methods (`platform/contract/packets.py`):

- `normalize(meta, raw) -> NormalizedPacket` — strip transport framing,
  label the frame type.
- `parse(normalized) -> MissionPacket` — decode into a payload plus a
  `mission` facts dict (safe to forward to clients and logs).
- `classify(packet) -> PacketFlags` — duplicate key, unknown flag,
  uplink-echo flag, CRC verdict.
- `match_verifiers(envelope, open_instances, *, now_ms, rx_event_id)` —
  return `[]` for RX-only missions.

`echo_v2/mission.py` is the minimal working reference (hex-dump payloads,
~15 lines). For a real decode, `astrocast/packets.py` (ASCII beacon) and
`sharjahsat/telemetry.py` (binary housekeeping) show the two common shapes.
If the bird speaks CSP over an AX100, don't write a new PacketOps — reuse
`ax100_rx.Ax100RxPacketOps` with an optional per-mission `hk_decoder`, as
the whole snipe/catsat/suomi100/roads family does.

### 3. Author `mission.yml`

The declarative database: parameter types, parameters, containers, and the
RX/TX table columns. Minimum useful skeleton:

```yaml
schema_version: 1
id: <id>
name: "<Display Name>"
header:
  version: "1.0.0"
  date: "YYYY-MM-DD"
  description: "<one line>"

parameter_types:
  volts_f: { kind: float, unit: "V" }

parameters:
  voltage: { type: volts_f, domain: beacon }

sequence_containers:
  beacon_hk:
    domain: beacon
    layout: ascii_tokens        # or binary layouts — see sharjahsat/catsat
    restriction_criteria:
      packet: { kind: beacon }
    entry_list:
      - { name: voltage }

ui:
  rx_columns:
    - { id: type, label: type, width: 52, path: header.type, badge: true }
    - { id: volt, label: volts, width: 56, align: right, path: beacon.voltage }
```

`astrocast/mission.yml` is a complete small example; `catsat` shows a
machine-generated table (with a guard test pinning yml ↔ field table).
TX missions add `meta_commands`, `verifier_specs`/`verifier_rules`, a
`framing:` chain, and `ui.tx_columns` — `maveric/mission.example.yml` is
the full reference.

**Tracked or gitignored?** Third-party RX missions track their
`mission.yml`. Only keep it gitignored (with a tracked
`mission.example.yml` template) when it carries non-public command sets —
the maveric pattern.

### 4. Write `build(ctx)` (`mission.py`)

```python
from pathlib import Path

from mav_gss_lib.platform import MissionConfigSpec, MissionContext, MissionSpec
from mav_gss_lib.platform.spec import parse_yaml

from mav_gss_lib.missions.<id>.packets import MyPacketOps
from mav_gss_lib.missions.<id>.tracking_defaults import seed_tracking_defaults

MISSION_YML_PATH = Path(__file__).resolve().parent / "mission.yml"

_MISSION_NAME = "<Display Name>"


def _seed(mission_cfg: dict, platform_cfg: dict) -> None:
    mission_cfg.setdefault("mission_name", _MISSION_NAME)
    rx = platform_cfg.setdefault("rx", {})
    rx.setdefault("frequency", "437.xxx MHz")
    radio = platform_cfg.setdefault("radio", {})
    radio.setdefault("script", "gnuradio/MAV_DUO.py")
    radio["decoder_yml"] = "gnuradio/decoders/<ID>_DECODER.yml"
    seed_tracking_defaults(platform_cfg)


def build(ctx: MissionContext) -> MissionSpec:
    _seed(ctx.mission_config, ctx.platform_config)
    return MissionSpec(
        id="<id>",
        name=ctx.mission_config.get("mission_name") or _MISSION_NAME,
        packets=MyPacketOps(),
        spec_root=parse_yaml(MISSION_YML_PATH, plugins={}),
        config=MissionConfigSpec(),
    )
```

Rules that matter here:

- **Required MissionSpec fields** are `id`, `name`, `packets`, `config`
  (enforced by `validate_mission_spec`). Everything else is optional —
  `commands=None` makes the platform reject TX admission with a clean
  error, which is exactly right for RX-only missions.
- **`spec_root` is required for server boot.** Any mission started through
  the web app must ship one — the alarm environment dereferences it at
  startup. Only pure test fixtures may omit it.
- **Seed with `setdefault`, never assign** — operator values from the
  config file must always win. The one exception is mission-owned keys the
  operator should not override (e.g. `radio["decoder_yml"]` above).
- **The platform owns no mission metadata.** Frequencies, TLE, station,
  radio script, mission name — all seeded here, from the mission side.
  Copy `astrocast/tracking_defaults.py` for the TLE/frequency/station
  seeding pattern (gap-fill only, pre-filled `tle_fetch.identifier` so
  operators can refresh in-app).

### 5. Radio path (usually zero new code)

Most RX missions ride the stock `gnuradio/MAV_DUO.py` flowgraph and only
ship a gr-satellites SatYAML database at
`gnuradio/decoders/<ID>_DECODER.yml`, selected by the seeded
`platform.radio.decoder_yml`. Add a decoder guard in
`tests/test_decoder_ymls.py` pinning its deviations. Only a genuinely
different RF chain warrants its own flowgraph (`MAV_ASTROCAST.py` is the
precedent).

### 6. Frontend (optional)

The web shell needs nothing for a basic mission — RX columns come from
`mission.yml::ui`. Mission-specific TX builders and pages are discovered
by convention from `mav_gss_lib/web/src/plugins/<id>/`
(`TxBuilder.tsx`, `plugins.ts`) — no registration step. If you add any,
run `npm run build` in `mav_gss_lib/web` and commit `dist/` with the
source.

### 7. Run it

```bash
python3 MAV_WEB.py --mission <id>
```

or in a running app: Config gear → Mission → Active Mission dropdown →
confirm (the server restarts into the mission). Verify it appears in
`GET /api/missions` first.

Each non-default mission reads and writes its own operator config,
`mav_gss_lib/gss.<id>.yml`, auto-seeded on first run. **Never select a
mission by hand-editing `mission.id` in `gss.yml`** — plain launches
always boot maveric by design, and a non-default mission running out of
`gss.yml` would overwrite MAVERIC's config on its next save.

### 8. Test it

Add `tests/test_mission_<id>.py`. House pattern: build the spec through
the real loader, then pin decode output against a **golden frame** — the
first real frame received off the air, byte-exact
(`test_mission_sharjahsat.py`, `test_roads_first_frame_golden`). If the
yml is generated from a field table, add a containers-match guard
(`test_catsat_yml_containers_match_field_table`). Then:

```bash
python3 -m unittest tests.test_mission_<id> tests.test_mission_switching tests.test_platform_mission_specs
```

## Which mission to copy

| You are building | Start from |
|---|---|
| Smallest possible working mission | `echo_v2` (fixture, no `mission.yml`) |
| RX-only, ASCII beacon | `astrocast` |
| RX-only, binary housekeeping | `sharjahsat` |
| AX100/CSP bird | `suomi100` or `roads` (via `ax100_rx`) |
| One mission covering several birds | `snipe` (per-bird swap table) |
| Large generated telemetry table | `catsat` |
| Full TX + verifiers + plugins | `maveric` |

## Checklist

- [ ] Folder name = mission id, lowercase, importable
- [ ] `mission.py` exports `build(ctx) -> MissionSpec`
- [ ] `spec_root` set (required for server boot)
- [ ] Defaults seeded via `setdefault` in `build(ctx)`
- [ ] `mission.yml` present ⇒ shows up in the switcher; absent ⇒ fixture/library
- [ ] Decoder database + `tests/test_decoder_ymls.py` guard (if riding MAV_DUO)
- [ ] `tests/test_mission_<id>.py` with a golden frame
- [ ] Mission `README.md` with protocol notes and enabling steps
