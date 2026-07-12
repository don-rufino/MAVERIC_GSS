# GNU Radio Webapp Launch Notes

The webapp Radio tab starts the configured GNU Radio Python flowgraph directly.
By default, it runs:

```bash
python -u gnuradio/MAV_DUO.py
```

It does not regenerate the flowgraph, run GNU Radio Companion, run `grcc`,
overwrite `MAV_DUO.py`, edit `MAV_DUO.py`, or change file
ownership/permissions. The Python file is opened by the Python interpreter as
input and executed.

GNU Radio Companion and `grcc` are different: they generate the Python
flowgraph from the `.grc` file and can rewrite the generated `.py` file. Manual
edits made directly in `MAV_DUO.py` can be lost the next time the flowgraph is
regenerated from `MAV_DUO.grc`.

## Linux Permissions

On Linux, the user running `MAV_WEB.py` does not need to own the flowgraph
Python file. It only needs:

- read permission on the configured flowgraph Python file
- execute/search permission on the parent directories
- read permission on supporting files used by the flowgraph: the selected
  `gnuradio/decoders/<MISSION>_DECODER.yml`, plus `gnuradio/decoder_profiles.py` for
  `MAV_DUO.py` mission-profile resolution

For example, this is sufficient even if another user owns the file:

```text
-rw-r--r--  someone  group  MAV_DUO.py
```

This would fail for other users because only the owner can read it:

```text
-rw-------  someone  group  MAV_DUO.py
```

The old overwrite problem can only happen when something edits or regenerates
the Python file from the `.grc` file, because overwriting or replacing a file
owned by another user requires write permission on the file or directory.

## Configured Flowgraph Path

The webapp uses `platform.radio.script` from `mav_gss_lib/gss.yml`.
If `mav_gss_lib/gss.yml` is missing, the built-in/default example value is:

```yaml
platform:
  radio:
    script: gnuradio/MAV_DUO.py
```

If someone regenerates the GNU Radio flowgraph under a different Python filename
or moves it to a different path, update `platform.radio.script` in
`mav_gss_lib/gss.yml` to match the new file. Otherwise the Radio tab will keep
launching the old configured path.

Keep `mav_gss_lib/gss.example.yml` in sync when changing the default path for
the whole station setup.
