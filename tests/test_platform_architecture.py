from pathlib import Path
import io
import re
import tokenize
import unittest


def test_platform_package_public_api_imports():
    import mav_gss_lib.platform as platform

    assert platform.MissionSpec
    assert platform.PlatformRuntime
    assert platform.RxPipeline
    assert platform.prepare_command
    assert platform.rx_packet_record
    assert platform.tx_command_record


def test_platform_package_does_not_import_maveric():
    root = Path("mav_gss_lib/platform")
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        if "missions.maveric" in text:
            offenders.append(str(path))

    assert offenders == []


def test_server_does_not_use_retired_adapter_boundary():
    root = Path("mav_gss_lib/server")
    forbidden = (
        "runtime.adapter",
        "load_mission_adapter",
        "MissionAdapter",
        "cmd_defs",
    )
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        for pattern in forbidden:
            if pattern in text:
                offenders.append((str(path), pattern))

    assert offenders == []


def test_maveric_v2_runtime_does_not_import_retired_adapter_boundary():
    root = Path("mav_gss_lib/missions/maveric")
    allowed = {"__init__.py", "README.md"}
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text()
        if "mission_adapter" in text or "ParsedPacket" in text:
            offenders.append(str(path))

    assert offenders == []


def test_no_flat_runtime_cfg_reads_in_server():
    """WebRuntime.cfg projection is retired; reads must use typed accessors
    or runtime.platform_cfg / runtime.mission_cfg directly."""
    root = Path("mav_gss_lib/server")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\bruntime\.cfg\b", line):
                offenders.append(f"{path}:{lineno}: {stripped}")

    assert offenders == [], "Flat-config reads found:\n  " + "\n  ".join(offenders)


def test_platform_package_does_not_encode_maveric_vocabulary():
    root = Path("mav_gss_lib/platform")
    forbidden = (
        "nodes",
        "ptypes",
        "gs_node",
        "imaging",
        "node_name",
        "ptype",
        "ptype_name",
        "resolve_node",
        "resolve_ptype",
    )
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.NAME and token.string in forbidden:
                offenders.append((str(path), token.string))

    assert offenders == []


class OutboundHttpConfinementTests(unittest.TestCase):
    def test_urllib_request_confined_to_fetch_module(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "mav_gss_lib"
        offenders = []
        for path in root.rglob("*.py"):
            if path.name == "fetch.py" and path.parent.name == "tracking":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "urllib.request" in text or "http.client" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"outbound HTTP must live in tracking/fetch.py only: {offenders}")
