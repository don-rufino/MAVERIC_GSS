"""MAVERIC beacon hex logger.

Appends one line per decoded gr-satellites frame to a log file:

    <ISO-8601 UTC timestamp> <hex>

Flushes after every frame so the file is safe to copy mid-pass and hand
to the MAVERIC ground team. No console output.
"""

import datetime

import pmt
from gnuradio import gr


class blk(gr.basic_block):
    """Append decoded frames as 'timestamp hex' lines to log_path."""

    def __init__(self, log_path='maveric_beacon.log'):
        gr.basic_block.__init__(
            self,
            name='MAVERIC Hex Logger',
            in_sig=[],
            out_sig=[],
        )
        self.log_path = log_path
        self.message_port_register_in(pmt.intern('pdu'))
        self.set_msg_handler(pmt.intern('pdu'), self.handle_pdu)

    def handle_pdu(self, msg):
        # A raised exception here would kill the message-handler thread and
        # stop logging for the rest of the pass, so drop malformed PDUs.
        try:
            if not pmt.is_pair(msg) or not pmt.is_u8vector(pmt.cdr(msg)):
                return
            frame = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
        except Exception:
            return
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec='milliseconds')
        with open(self.log_path, 'a') as handle:
            handle.write('{} {}\n'.format(stamp, frame.hex()))
