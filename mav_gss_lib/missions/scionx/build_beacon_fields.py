"""Build SCION-X's local beacon field map from the team's spreadsheet.

The beacon telemetry layout is team-supplied and stays out of the
repository: this writes ``beacon_fields.json`` (gitignored) next to this
file from sheet ``Tlm_Beacon`` of the team workbook. Both the live decoder
(gnuradio/scionx_burst.py, plausibility scoring) and the mission's packet
parser (telemetry values) read that file; without it the mission still
receives and logs frames, but shows no telemetry values.

    python3 -m mav_gss_lib.missions.scionx.build_beacon_fields SCIONX-CMD-TLM-Beacon.xlsx

Copy the resulting JSON to every GSS machine that runs SCION-X.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

OUT_PATH = Path(__file__).resolve().parent / "beacon_fields.json"
SHEET = "Tlm_Beacon"
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_sheet(path: str | Path, name: str) -> list[list[str]]:
    """Cell values of one sheet as rows (stdlib only)."""
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid = next(s.get("{%s}id" % _NS["r"]) for s in wb.find("m:sheets", _NS) if s.get("name") == name)
    target = next(r.get("Target") for r in rels if r.get("Id") == rid)
    sheet = ET.fromstring(z.read("xl/" + target.lstrip("/").removeprefix("xl/")))
    rows = []
    for row in sheet.iter("{%s}row" % _NS["m"]):
        cells = {}
        for c in row.findall("m:c", _NS):
            v = c.find("m:v", _NS)
            if c.get("t") == "s" and v is not None:
                val = shared[int(v.text)]
            elif c.get("t") == "inlineStr":
                val = "".join(t.text or "" for t in c.iter("{%s}t" % _NS["m"]))
            else:
                val = v.text if v is not None else None
            if val not in (None, ""):
                cells[_col_index(c.get("r"))] = val
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


def beacon_fields(xlsx: str | Path) -> list[dict]:
    """Field rows: name, ctype, size, offset (from the APID), endian, var.
    Repeated sub-fields inherit the last named row's name."""
    out: list[dict] = []
    last_name = ""
    for r in read_sheet(xlsx, SHEET):
        if len(r) < 10:
            continue
        name, ctype, size, offset = r[5], r[6], r[8], r[9]
        endian = r[10] if len(r) > 10 else ""
        var = r[11] if len(r) > 11 else ""
        if not str(offset).isdigit() or not str(ctype).endswith(("_t", "float")):
            continue
        name = str(name or last_name).strip()
        last_name = name
        out.append({"name": name, "var": str(var).strip() or name, "ctype": str(ctype),
                    "size": int(size), "offset": int(offset), "endian": str(endian) or "Little"})
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__.strip().splitlines()[0])
        print("usage: python3 -m mav_gss_lib.missions.scionx.build_beacon_fields <workbook.xlsx>")
        return 2
    fields = beacon_fields(args[0])
    OUT_PATH.write_text(json.dumps({"source": Path(args[0]).name, "sheet": SHEET,
                                    "info_offset": 16, "fields": fields}, indent=1),
                        encoding="utf-8")
    print(f"wrote {len(fields)} fields to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
