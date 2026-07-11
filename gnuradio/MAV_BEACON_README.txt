MAVERIC Open Beacon Decoder  (SatNOGS-style)
============================================

Decode the MAVERIC CubeSat UHF beacon and send us the result. Thank you for
helping track MAVERIC and keep its status updated.

Beacon (at launch, may change over time)
  Center frequency : 437.575 MHz (UHF)
  Modulation       : GFSK (FSK)
  Framing          : AX100 Mode 5 - ASM + Golay
  Baud rate        : 9600 (nominal)
  Bandwidth        : ~20 kHz
  Antenna          : circular polarization
  Beacon interval  : ~3 minutes, intermittent over the orbit


INSTALL  (one time)
  You need GNU Radio 3.10+ with gr-satellites and gr-soapy. The easiest way to
  get all of them in one step is the radioconda distribution:

    1. Download and install radioconda for your OS:
         https://github.com/ryanvolz/radioconda/releases/latest
       (accept the defaults; it installs into your home directory and does not
        touch your system Python)
    2. Open a new terminal (radioconda's environment auto-activates) and verify:
         gr_satellites --version
       If that prints a version, you are ready.

  RTL-SDR users: plug the dongle in before launching. On Linux you may need the
  usual rtl-sdr udev rules / to blacklist the dvb_usb_rtl28xxu kernel module.


CONTENTS  (keep these files together in one folder)
  MAV_BEACON.py              <- run this
  MAV_BEACON_epy_block_0.py  <- helper module (imported by MAV_BEACON.py)
  MAVERIC_BEACON.yml         <- decoder definition (9k6 FSK AX100 ASM+Golay)
  MAV_BEACON.grc             <- open in GNU Radio Companion to edit (optional)
  README.txt                 <- this file


RUN -- live, with an RTL-SDR
  Plug in the dongle, connect your UHF antenna, then:

      python3 MAV_BEACON.py

  It tunes to 437.575 MHz automatically. You can launch it from any directory;
  it locates its own files. Every decoded frame is appended to

      maveric_beacon.log        (created next to MAV_BEACON.py)

  as one line:   <UTC timestamp>  <hex frame>
  Press Ctrl+C to stop after the pass.


RUN -- decode a recorded IQ file (no live hardware)
  Open MAV_BEACON.grc in GNU Radio Companion. Select the "File Source" and
  "Throttle" blocks and ENABLE them (Ctrl+E); select the "Soapy RTLSDR Source"
  and DISABLE it (Ctrl+D). Set the iq_file and samp_rate variables to match your
  recording (complex/IQ), then run. Output goes to maveric_beacon.log as above.


ADJUSTABLE SETTINGS  (variables in the .grc, or near the top of MAV_BEACON.py)
  freq         437.575e6   center frequency, Hz
  samp_rate    250000      SDR / IQ sample rate, Hz
  gain         40          RTL-SDR RF gain, dB
  freq_corr    0           RTL-SDR crystal calibration, PPM
  freq_offset  0           manual tune nudge, Hz (see "Frequency / Doppler")


FREQUENCY / DOPPLER -- if you see a signal but get no decodes
  MAVERIC transmits a fixed 437.575 MHz. Because the satellite is moving
  relative to you, YOUR receiver sees that carrier Doppler-shifted by up to
  ~+/-10 kHz over a pass (highest as it approaches, dropping through 437.575 MHz
  at closest approach, lowest as it recedes). Your ground station has to account
  for that shift. The decoder tolerates roughly +/-10 kHz on its own, so a fixed
  tune at 437.575 MHz decodes across most of a typical pass -- best near maximum
  elevation, where the received frequency is closest to 437.575 MHz. If you run
  Doppler tracking (e.g. Gpredict steering your SDR), even better.

  A more common reason for "signal but no decode" is an uncalibrated RTL-SDR
  whose crystal is off by several kHz. Two knobs fix that:
    - freq_corr (PPM): your dongle's known calibration error (1 PPM ~= 437 Hz).
    - freq_offset (Hz): a direct manual nudge, e.g. freq_offset = 5000 to listen
      5 kHz above 437.575 MHz.
  Leave both at 0 if you are decoding fine.


DOPPLER TRACKING WITH GPREDICT  (optional, for full-pass coverage)
  To decode across the whole pass instead of just near closest approach, let
  Gpredict retune the decoder in real time. MAV_BEACON.py listens for Gpredict
  on 127.0.0.1 port 7356 (this uses gr-gpredict-doppler, which is included in
  radioconda). You also need the Gpredict application itself -- https://oz9aec.dk
  /gpredict/ or your OS package manager -- with MAVERIC's TLE and your station
  location loaded.

    1. Gpredict -> Edit -> Preferences -> Interfaces -> Radios -> Add New:
         Host = localhost,  Port = 7356,  Type = "RX only".  Save.
    2. Start the decoder:   python3 MAV_BEACON.py
    3. Open Gpredict's "Radio Control" window, select the radio you just added
       and the MAVERIC satellite, set the downlink to 437.575 MHz, then click
       Engage and enable Track.

  Gpredict now streams the Doppler-corrected frequency to the decoder, which
  retunes the RTL-SDR automatically for the whole pass. Leave freq_offset = 0
  while Gpredict is driving (still set freq_corr for your dongle's PPM error).
  If you do not use Gpredict, nothing changes -- the decoder just listens on
  that port and is otherwise unaffected.


SEND US YOUR CATCH
  E-mail the maveric_beacon.log file (or paste the hex lines) to

      maveric@isi.edu

  Please include: the observation time in UTC, your location / Maidenhead grid,
  and the frequency you observed. That's it -- we'll handle the rest.
