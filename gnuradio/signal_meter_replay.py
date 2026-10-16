"""Replay a MAV_DUO SigMF capture through the live signal meter.

Feeds the recorded 200 ksps channel (the same FIR output the live meter's
display tap decimates) through `signal_meter.SignalMeter` exactly as MAV_DUO
does: decimated by the live `disp_decim`, Doppler retunes from the capture's
`doppler_command` records, and one measurement per decoded frame. Frames come
from the profile's own decoder — the SCION-X burst decoder, or the mission's
gr-satellites database — so each measurement is triggered the way it is live.

Time is the capture's sample clock (USRP rx_time, re-anchored at every
capture record), not host wall time, so sample drops never misplace a row.

With --session, every measurement is paired with the live `signal` block of
the session log's rx_packet records (nearest burst end within 2 s) and the
differences are printed.

    python3 gnuradio/signal_meter_replay.py logs/iq/<capture>.sigmf-data \
        [--session logs/json/session_<...>.jsonl] [--out replay.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import signal_meter  # noqa: E402
from decoder_profiles import burst_decoder, decoder_options, tx_baudrate  # noqa: E402

CHUNK = 8192                 # 200 ksps samples per push (scheduler-sized)
DEFAULT_RX_BW_HZ = 50_000.0  # MAV_DUO rx_bw when the capture does not say
SCIONX_MIN_BURST_S = 0.1     # MAV_DUO _ScionxBurstDecoder beacon filter
SCIONX_MAX_BURST_S = 0.8
MATCH_WINDOW_S = 2.0


def _utc(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


class CaptureClock:
    """Sample index -> UTC seconds from the SigMF capture records."""

    def __init__(self, captures: list[dict], fs: float) -> None:
        self.fs = fs
        self.records = sorted((c for c in captures if c.get("core:datetime")),
                              key=lambda c: c["core:sample_start"])
        self.starts = np.array([c["core:sample_start"] for c in self.records], dtype=np.int64)
        self.times = np.array([_utc(c["core:datetime"]) for c in self.records])

    def __call__(self, sample: int) -> float:
        i = max(0, int(np.searchsorted(self.starts, sample, side="right")) - 1)
        return float(self.times[i] + (sample - self.starts[i]) / self.fs)


def display_decim(fs: float, rx_bw: float) -> int:
    # MAV_DUO: disp_decim = max(1, min(20, int((samp_rate / (2 * rx_decim)) // rx_bw)))
    return max(1, min(20, int((fs / 2) // rx_bw)))


# ------------------------------------------------------------ frame triggers

def gr_satellites_frames(iq: Path, decoder_yml: Path) -> list[tuple[int, int]]:
    """(sample position at PDU emission, payload length) for every frame the
    mission's gr-satellites database decodes. Small scheduler buffers keep
    the emission position within a few ms of where the live PDU would land."""
    from gnuradio import blocks, gr
    import pmt
    from satellites.core.gr_satellites_flowgraph import gr_satellites_flowgraph

    class Counter(gr.sync_block):
        def __init__(self):
            gr.sync_block.__init__(self, name="sample_counter",
                                   in_sig=[np.complex64], out_sig=[np.complex64])
            self.position = 0

        def work(self, input_items, output_items):
            n = len(input_items[0])
            output_items[0][:] = input_items[0]
            self.position += n
            return n

    class Collect(gr.basic_block):
        def __init__(self, counter):
            gr.basic_block.__init__(self, name="pdu_collect", in_sig=[], out_sig=[])
            self.counter = counter
            self.frames: list[tuple[int, int]] = []
            self.message_port_register_in(pmt.intern("in"))
            self.set_msg_handler(pmt.intern("in"), self._pdu)

        def _pdu(self, msg):
            self.frames.append((self.counter.position, pmt.length(pmt.cdr(msg))))

    tb = gr.top_block()
    src = blocks.file_source(gr.sizeof_gr_complex, str(iq), False)
    counter = Counter()
    collect = Collect(counter)
    fg = gr_satellites_flowgraph(file=str(decoder_yml), samp_rate=200000, iq=True,
                                 grc_block=True, options=decoder_options(decoder_yml))
    tb.connect(src, counter, fg)
    tb.msg_connect((fg, "out"), (collect, "in"))
    tb.start(512)
    tb.wait()
    time.sleep(1.0)  # let the last deframer messages drain
    return sorted(collect.frames)


# ------------------------------------------------------------ replay

def replay(iq: Path, nominal_hz: float | None, rx_bw: float,
           decoder: str | None = None) -> tuple[list[dict], dict]:
    meta = json.loads(iq.with_suffix(".sigmf-meta").read_text(encoding="utf-8"))
    glob = meta["global"]
    fs = float(glob["core:sample_rate"])
    decoder_yml = HERE / "decoders" / (decoder or glob.get("maveric:decoder_yml", ""))
    if not decoder_yml.is_file():
        raise SystemExit(f"decoder database not found: {decoder_yml}")
    if nominal_hz is None:
        import yaml
        profile = yaml.safe_load(decoder_yml.read_text(encoding="utf-8"))
        nominal_hz = float(next(iter(profile["transmitters"].values()))["frequency"])
    baud = float(tx_baudrate(decoder_yml))   # what MAV_DUO hands the live meter
    decim = display_decim(fs, rx_bw)
    clock = CaptureClock(meta["captures"], fs)
    x = np.memmap(iq, dtype=np.complex64, mode="r")

    meter = signal_meter.SignalMeter(fs / decim, nominal_hz=nominal_hz)
    tunes = []
    for c in sorted(meta["captures"], key=lambda c: c["core:sample_start"]):
        event = c.get("maveric:event")
        # the opening record is the initial tune; discontinuity records only
        # re-anchor time and repeat the current frequency
        if "core:frequency" in c and c.get("core:datetime") and event in (None, "doppler_command"):
            source = "doppler" if event == "doppler_command" else "manual"
            tunes.append((c["core:sample_start"], _utc(c["core:datetime"]), float(c["core:frequency"]), source))

    scionx = burst_decoder(decoder_yml) == "scionx"
    if scionx:
        import scionx_burst
        collector = scionx_burst.BurstCollector(fs)
        scorer = scionx_burst.FieldScorer.from_json(scionx_burst._default_fields_path())
        frames: list[tuple[int, int]] = []
    else:
        frames = gr_satellites_frames(iq, decoder_yml)

    results: list[dict] = []

    def measure(trigger_sample: int, now: float, payload_len: int, extra: dict) -> None:
        r = meter.measure(payload_len * 8.0 / baud, baud, now)
        results.append({"trigger_s": round(trigger_sample / fs, 3), "trigger_utc": now,
                        "payload_len": payload_len, **extra, "signal": r})

    # chunk boundaries also break at every capture record so a chunk never
    # straddles a time discontinuity
    cuts = sorted({c["core:sample_start"] for c in meta["captures"]} | {len(x)})
    tune_i = frame_i = 0
    pos = 0
    for cut in cuts:
        while pos < cut:
            end = min(cut, pos + CHUNK)
            while tune_i < len(tunes) and tunes[tune_i][0] <= pos:
                _, t, hz, source = tunes[tune_i]
                meter.note_tune(t, hz, source)
                tune_i += 1
            first = pos + (-pos) % decim          # keep_one_in_n phase follows the global index
            chunk = np.asarray(x[pos:end])
            meter.push(chunk[first - pos::decim], clock(end))
            if scionx:
                for seg, seg_start, _ in collector.push(chunk):
                    for a, b, result in scionx_burst.decode_segment(seg, fs, scorer):
                        dur = (b - a) / fs
                        beacon_like = (SCIONX_MIN_BURST_S <= dur <= SCIONX_MAX_BURST_S
                                       and abs(result.bit_rate - scionx_burst.BAUD) < 0.01 * scionx_burst.BAUD
                                       and result.transitions >= 40)
                        if result.status == "undecoded" and not beacon_like:
                            continue
                        end_sample = seg_start + b
                        measure(end_sample, clock(end_sample) + 0.1, len(result.frame),
                                {"decode": result.status, "start_byte": result.start_byte})
            while frame_i < len(frames) and frames[frame_i][0] <= end:
                sample, length = frames[frame_i]
                measure(sample, clock(sample), length, {"decode": "gr-satellites"})
                frame_i += 1
            pos = end

    info = {"capture": iq.name, "mission": glob.get("maveric:mission"), "fs": fs,
            "meter_fs": fs / decim, "disp_decim": decim, "baud": baud,
            "nominal_hz": nominal_hz, "decoder": decoder_yml.name,
            "tunes": len(tunes), "frames": len(results)}
    return results, info


# ------------------------------------------------------------ live comparison

def live_measurements(session: Path) -> list[dict]:
    out = []
    with session.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            sig = rec.get("signal")
            if rec.get("event_kind") == "rx_packet" and isinstance(sig, dict) and sig.get("status") == "ok":
                out.append({"ts_iso": rec.get("ts_iso"), "seq": rec.get("seq"), "signal": sig})
    return out


COMPARE = (("center_offset_hz", "residual Hz"), ("tune.tuned_hz", "tuned Hz"),
           ("nominal_offset_hz", "Δf nom Hz"), ("snr_3db_db", "SNR-3dB"),
           ("bw_levels.10.bw_hz", "-10dB BW"), ("bw_levels.20.bw_hz", "-20dB BW"),
           ("peak_dbfs", "peak dBFS"), ("floor_dbfs", "floor dBFS"), ("burst_s", "burst s"))


def _get(sig: dict, path: str):
    for key in path.split("."):
        sig = sig.get(key) if isinstance(sig, dict) else None
    return sig


def _fmt(v) -> str:
    return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def print_replay(results: list[dict], info: dict) -> None:
    print(json.dumps(info))
    for r in results:
        s = r["signal"] or {}
        when = datetime.fromtimestamp(r["trigger_utc"], timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        print(f"{r['trigger_s']:9.3f} s {when}Z {r['decode']:<13} {r['payload_len']:4d} B  "
              + "  ".join(f"{label} {_fmt(_get(s, key))}" for key, label in COMPARE)
              + f"  floor:{s.get('floor_method', s.get('status', '-'))}")


def print_comparison(results: list[dict], live: list[dict]) -> None:
    print(f"\nlive vs replay ({len(live)} live measurements, {len(results)} replayed)")
    used = set()
    for r in results:
        s = r["signal"] or {}
        end = s.get("burst_end_ms")
        best = None
        if end is not None:
            for i, l in enumerate(live):
                d = abs(l["signal"].get("burst_end_ms", 0) - end) / 1000
                if i not in used and d <= MATCH_WINDOW_S and (best is None or d < best[1]):
                    best = (i, d)
        when = datetime.fromtimestamp(r["trigger_utc"], timezone.utc).strftime("%H:%M:%S")
        if best is None:
            print(f"  {when}Z replay only (no live measurement within {MATCH_WINDOW_S:.0f} s)")
            continue
        used.add(best[0])
        l = live[best[0]]["signal"]
        print(f"  {when}Z  Δt(burst end) {best[1] * 1000:+.0f} ms   seq {live[best[0]]['seq']}")
        for key, label in COMPARE:
            a, b = _get(l, key), _get(s, key)
            diff = f"{b - a:+.1f}" if isinstance(a, (int, float)) and isinstance(b, (int, float)) else ""
            print(f"      {label:<11} live {_fmt(a):>13}  replay {_fmt(b):>13}  {diff}")
    for i, l in enumerate(live):
        if i not in used:
            print(f"  {l['ts_iso']} live only (seq {l['seq']}): no replayed frame matched")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("iq", type=Path, help="<capture>.sigmf-data")
    ap.add_argument("--session", type=Path, help="session JSONL from the same pass")
    ap.add_argument("--nominal", type=float, help="nominal RX Hz (default: decoder profile)")
    ap.add_argument("--decoder", help="decoder database in gnuradio/decoders (default: the "
                    "one recorded in the capture, e.g. SCIONX_DECODER.yml for a SCION-X pass "
                    "recorded under another mission)")
    ap.add_argument("--rx-bw", type=float, help="rx_bw Hz (default: capture metadata, else 50 kHz)")
    ap.add_argument("--out", type=Path, help="write replayed measurements as JSONL")
    args = ap.parse_args(argv)
    meta = json.loads(args.iq.with_suffix(".sigmf-meta").read_text(encoding="utf-8"))
    rx_bw = args.rx_bw or float(meta["global"].get("maveric:rx_bw_hz") or DEFAULT_RX_BW_HZ)
    results, info = replay(args.iq, args.nominal, rx_bw, args.decoder)
    print_replay(results, info)
    if args.out:
        with args.out.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
    if args.session:
        print_comparison(results, live_measurements(args.session))
    sys.stdout.flush()
    os._exit(0)  # gr-satellites teardown can hang after tb.wait()


if __name__ == "__main__":
    main()
