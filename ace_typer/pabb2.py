"""Minimal client for Pokémon Automation's PABotBase2 serial protocol.

Drives an ESP32-S3 flashed with PA's PABotBase2 firmware (the board PA uses as
a wired Pro Controller). Ported from PokemonAutomation/Arduino-Source:
Common/PABotBase2 (packets, CRC, stream) and
SerialPrograms/Source/Controllers/PABotBase2 (messages, command queue).

Only what ace-typer needs is here:
- one packet in flight at a time (stop-and-wait), retransmitted until acked;
- the reliable byte stream that carries messages both ways;
- requests (version, status, ...) and the board's command queue.

The board runs every queued command for its exact duration and reports when
each one finishes, so a stalled host cannot shorten, merge or drop a press.
"""

import os
import struct
import threading
import time

# ---- Packet layer (PABotBase2_PacketProtocol.h) ------------------------------

MAGIC = 0x81
OPCODE_MASK = 0x7F
CONNECTION_PROTOCOL = 2026061800

ASK_RESET = 0x01
ASK_VERSION = 0x02
ASK_PACKET_SIZE = 0x03
ASK_STREAM_DATA = 0x12
RET_RESET = 0x41
RET_VERSION = 0x42
RET_PACKET_SIZE = 0x43
RET_STREAM_DATA = 0x52
INFO_STR = 0x25
INVALID_LENGTH = 0x30
INVALID_CHECKSUM = 0x31
UNKNOWN_OPCODE = 0x32

HEADER = 4          # magic, seqnum, packet_bytes, opcode
CRC = 4
MIN_PACKET = HEADER + CRC
MAX_PACKET = 256    # packet_bytes 0 means 256
STREAM_OVERHEAD = HEADER + 2 + CRC   # + u16 stream_offset
STREAM_FREE_BYTES = 16384            # what we report back in stream acks

# ---- Message layer (PABotBase2_MessageProtocol.h) ----------------------------

MESSAGE_PROTOCOL = 2026061804

MSG_LOG_STRING = 0x01
MSG_REQUEST_DROPPED = 0x10
MSG_RET = 0x11
MSG_RET_U32 = 0x12
MSG_RET_DATA = 0x13
MSG_RET_U32_DATA = 0x14
MSG_PROTOCOL_VERSION = 0x20
MSG_FIRMWARE_VERSION = 0x21
MSG_DEVICE_NAME = 0x23
MSG_CQ_CAPACITY = 0x26
MSG_REQUEST_STATUS = 0x31
MSG_READ_CONTROLLER_MODE = 0x32
MSG_CONSOLE_DISCONNECT = 0x37
MSG_CQ_COMMAND_DROPPED = 0x40
MSG_CQ_CANCEL = 0x41
MSG_CQ_COMMAND_FINISHED = 0x43
MSG_NS1_PLAYER_LIGHTS = 0x94
MSG_NS1_USB_DISALLOWED = 0x95
MSG_NS1_RUMBLE = 0x96
MSG_NS1_BUTTONS = 0x97

RET_OPCODES = (MSG_RET, MSG_RET_U32, MSG_RET_DATA, MSG_RET_U32_DATA)

# SerialPABotBase_Protocol_IDs.h
CID_NS1_WIRED_PRO_CONTROLLER = 0x1100


class ProtocolError(RuntimeError):
    pass


# ---- CRC32C, seeded, no final XOR (Common/CRC32/pabb_CRC32_Basic.c) ----------

def _crc_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


CRC_TABLE = _crc_table()


def crc32c(seed, data):
    c = seed
    for b in data:
        c = CRC_TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c


def encode_packet(session, seq, opcode, payload=b""):
    size = MIN_PACKET + len(payload)
    if size > MAX_PACKET:
        raise ValueError(f"packet of {size} bytes is over {MAX_PACKET}")
    body = bytes((MAGIC, seq & 0xFF, size & 0xFF, opcode)) + payload
    # A reset packet is checked with 0xFFFFFFFF: the board does not know the
    # new session ID yet.
    seed = 0xFFFFFFFF if opcode & OPCODE_MASK == ASK_RESET else session
    return body + struct.pack("<I", crc32c(seed, body))


class PacketParser:
    """Bytes in, (seqnum, opcode, payload) out. Skips noise and bad CRCs."""

    def __init__(self):
        self.buf = bytearray()
        self.bad = 0

    def reset(self):
        self.buf.clear()

    def feed(self, data, session):
        self.buf += data
        packets = []
        while True:
            start = self.buf.find(MAGIC)
            if start < 0:
                self.buf.clear()
                return packets
            del self.buf[:start]
            if len(self.buf) < MIN_PACKET:
                return packets
            size = self.buf[2] or MAX_PACKET
            if size < MIN_PACKET:
                self.bad += 1
                del self.buf[:1]
                continue
            if len(self.buf) < size:
                return packets
            pkt = bytes(self.buf[:size])
            opcode = pkt[3]
            seed = 0xFFFFFFFF if opcode & OPCODE_MASK == ASK_RESET else session
            if crc32c(seed, pkt[:-CRC]) != struct.unpack_from("<I", pkt, size - CRC)[0]:
                # Not a packet (or a damaged one): resync after this byte.
                self.bad += 1
                del self.buf[:1]
                continue
            del self.buf[:size]
            packets.append((pkt[1], opcode, pkt[HEADER:-CRC]))

    def idle(self, session):
        """The line went quiet: the board sends whole packets, so bytes still
        waiting for their packet to complete are noise (a stray magic byte
        with a large length). Rescan past them."""
        packets = []
        while self.buf:
            self.bad += 1
            del self.buf[:1]
            packets += self.feed(b"", session)
        return packets


# ---- Reliable stream over the serial port ------------------------------------

class Link:
    """Reliable connection to the board (PABotBase2CC_ReliableStreamConnection,
    with one unacked packet at a time).

    `port` is a pyserial-like object: read(n) returning b"" on timeout,
    write(data), and a settable `baudrate`.
    """

    BAUD_RATES = (921600, 115200)

    def __init__(self, port, ack_timeout=0.1, retries=50, log=None):
        self.port = port
        self.ack_timeout = ack_timeout
        self.retries = retries
        self.log = log or (lambda msg: None)
        self.on_stream = lambda data: None
        self.session = 0
        self.seq = 0
        self.max_packet = 24
        self.out_offset = 0
        self.in_seq = 0
        self.in_offset = 0
        self.in_future = {}
        self.parser = PacketParser()
        self.error = None
        self.retransmits = 0
        self._send_lock = threading.Lock()   # one packet in flight
        self._write_lock = threading.Lock()  # sender and reader both write
        self._cv = threading.Condition()
        self._waiting = None                 # (seqnum, ret opcode)
        self._ack = None
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- setup

    def connect(self, timeout=3.0):
        """Find the baud rate, start a new session, check the protocol."""
        deadline = time.monotonic() + timeout
        while True:
            for baud in self.BAUD_RATES:
                self.port.baudrate = baud
                if self.reset(tries=1):
                    self.log(f"board answered at {baud} baud, session {self.session:08x}")
                    break
            else:
                if time.monotonic() < deadline:
                    continue
                raise ProtocolError(
                    "the board does not answer. Is it flashed with PABotBase2, "
                    "and is the COM port the one connected?")
            break
        version = struct.unpack("<I", self._transact(ASK_VERSION, b"", RET_VERSION))[0]
        if (version // 100 != CONNECTION_PROTOCOL // 100
                or version % 100 < CONNECTION_PROTOCOL % 100):
            raise ProtocolError(
                f"connection protocol {version} is not compatible with "
                f"{CONNECTION_PROTOCOL}; flash the firmware that matches this code")
        size = struct.unpack("<I", self._transact(ASK_PACKET_SIZE, b"", RET_PACKET_SIZE))[0]
        self.max_packet = min(size or MAX_PACKET, MAX_PACKET)
        if self.max_packet <= STREAM_OVERHEAD:
            raise ProtocolError(f"board packet size {size} is too small")
        return version

    def reset(self, tries=None):
        """Start a new session. True when the board acked it."""
        with self._send_lock:
            with self._cv:
                session = struct.unpack("<I", os.urandom(4))[0]
                self.session = session if session != 0xFFFFFFFF else 1
                self.seq = 0
                self.out_offset = 0
                self.in_seq = 0
                self.in_offset = 0
                self.in_future.clear()
                self.parser.reset()
            try:
                self._transact_locked(ASK_RESET, struct.pack("<I", self.session),
                                      RET_RESET, tries)
            except ProtocolError:
                return False
            return True

    def close(self):
        self._closed = True
        self._reader.join(timeout=1)

    # -- sending

    def send_stream(self, data):
        """Send bytes on the reliable stream. Returns after the board acked them."""
        step = self.max_packet - STREAM_OVERHEAD
        for i in range(0, len(data), step):
            part = data[i:i + step]
            with self._send_lock:
                payload = struct.pack("<H", self.out_offset) + part
                self._transact_locked(ASK_STREAM_DATA, payload, RET_STREAM_DATA)
                self.out_offset = (self.out_offset + len(part)) & 0xFFFF

    def _transact(self, opcode, payload, ret_opcode):
        with self._send_lock:
            return self._transact_locked(opcode, payload, ret_opcode)

    def _transact_locked(self, opcode, payload, ret_opcode, tries=None):
        seq = self.seq
        packet = encode_packet(self.session, seq, opcode, payload)
        with self._cv:
            self._waiting = (seq, ret_opcode)
            self._ack = None
        for attempt in range(tries or self.retries):
            if self.error:
                raise ProtocolError(self.error)
            if attempt:
                self.retransmits += 1
            self._write(packet)
            with self._cv:
                if self._cv.wait_for(lambda: self._ack is not None or self.error,
                                     timeout=self.ack_timeout):
                    if self._ack is not None:
                        ack = self._ack
                        self._waiting = None
                        self.seq = (seq + 1) & 0xFF
                        return ack
        with self._cv:
            self._waiting = None
        raise ProtocolError(f"no ack from the board for opcode 0x{opcode:02x} (seq {seq})")

    def _write(self, data):
        with self._write_lock:
            self.port.write(data)

    # -- receiving

    def _read_loop(self):
        while not self._closed:
            try:
                data = self.port.read(256)
            except Exception as e:  # port closed or unplugged
                if not self._closed:
                    self._fail(f"serial port error: {e}")
                return
            packets = (self.parser.feed(data, self.session) if data
                       else self.parser.idle(self.session))
            for seq, opcode, payload in packets:
                self._on_packet(seq, opcode, payload)

    def _fail(self, message):
        with self._cv:
            if self.error is None:
                self.error = message
            self._cv.notify_all()

    def _on_packet(self, seq, opcode, payload):
        op = opcode & OPCODE_MASK
        if op in (RET_RESET, RET_VERSION, RET_PACKET_SIZE, RET_STREAM_DATA):
            with self._cv:
                if self._waiting == (seq, op):
                    self._ack = payload
                    self._cv.notify_all()
        elif op == ASK_STREAM_DATA:
            self._on_stream_packet(seq, payload)
        elif op == UNKNOWN_OPCODE:
            self._fail(f"the board does not know opcode {payload!r}")
        elif op in (INVALID_LENGTH, INVALID_CHECKSUM):
            self.log(f"board rejected packet {seq} (0x{op:02x}); it will be resent")
        elif op == INFO_STR:
            self.log(f"board: {payload.decode('ascii', 'replace')}")

    def _ack_stream(self, seq):
        self._write(encode_packet(self.session, seq, RET_STREAM_DATA,
                                  struct.pack("<I", STREAM_FREE_BYTES)))

    def _on_stream_packet(self, seq, payload):
        if len(payload) < 2:
            return
        offset = struct.unpack_from("<H", payload)[0]
        data = payload[2:]
        ahead = (seq - self.in_seq) & 0xFF
        if ahead >= 128:
            # Already delivered; the board missed our ack.
            self._ack_stream(seq)
            return
        self.in_future[seq] = (offset, data)
        self._ack_stream(seq)
        while self.in_seq in self.in_future:
            offset, data = self.in_future.pop(self.in_seq)
            if data and offset != self.in_offset:
                self._fail(f"stream offset {offset} != expected {self.in_offset}")
                return
            self.in_seq = (self.in_seq + 1) & 0xFF
            self.in_offset = (self.in_offset + len(data)) & 0xFFFF
            if data:
                self.on_stream(data)


# ---- Messages, requests and the command queue --------------------------------

class Board:
    """Message layer (PABotBase2_DeviceHandle + CommandQueueManager)."""

    def __init__(self, link, log=None):
        self.link = link
        self.log = log or (lambda msg: None)
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._responses = {}
        self._pending_requests = set()
        self._request_id = 0
        self._command_id = 0
        self.pending_commands = {}
        self.finished = []          # (command id, board timestamp), in order
        self.errors = []
        self.player_lights = None
        self.cancelled = False
        # Orders queued commands against a cancel on the wire, so no command
        # can slip in after the cancel that was meant to stop it.
        self._wire = threading.Lock()
        link.on_stream = self._on_bytes

    # -- incoming

    def _on_bytes(self, data):
        self._buf += data
        while len(self._buf) >= 4:
            size = struct.unpack_from("<H", self._buf)[0]
            if size < 4:
                self._error(f"corrupted message stream (length {size})")
                self._buf.clear()
                return
            if len(self._buf) < size:
                return
            msg = bytes(self._buf[:size])
            del self._buf[:size]
            self._on_message(msg[2], msg[3], msg[4:])

    def _error(self, message):
        with self._cv:
            self.errors.append(message)
            self._cv.notify_all()

    def _on_message(self, opcode, mid, body):
        if opcode in RET_OPCODES or opcode == MSG_REQUEST_DROPPED:
            with self._cv:
                if mid in self._pending_requests:
                    self._responses[mid] = (opcode, body)
                    self._cv.notify_all()
        elif opcode == MSG_CQ_COMMAND_FINISHED:
            stamp = struct.unpack_from("<I", body)[0] if len(body) >= 4 else None
            with self._cv:
                if self.pending_commands.pop(mid, None) is not None:
                    self.finished.append((mid, stamp))
                self._cv.notify_all()
        elif opcode == MSG_CQ_COMMAND_DROPPED:
            self._error(f"the board dropped command {mid}")
        elif opcode == MSG_CONSOLE_DISCONNECT:
            self._error("the Switch disconnected the controller")
        elif opcode == MSG_NS1_USB_DISALLOWED:
            self._error('turn on "Pro Controller Wired Communication" in the Switch settings')
        elif opcode == MSG_NS1_PLAYER_LIGHTS:
            if len(body) >= 4:
                self.player_lights = struct.unpack_from("<I", body)[0]
        elif opcode == MSG_LOG_STRING:
            self.log(f"board: {body.decode('ascii', 'replace')}")

    # -- requests

    def request(self, opcode, body=b"", timeout=2.0):
        with self._cv:
            while self._request_id in self._pending_requests:
                self._request_id = (self._request_id + 1) & 0xFF
            mid = self._request_id
            self._request_id = (mid + 1) & 0xFF
            self._pending_requests.add(mid)
        try:
            self.link.send_stream(struct.pack("<HBB", 4 + len(body), opcode, mid) + body)
            with self._cv:
                if not self._cv.wait_for(lambda: mid in self._responses or self.errors,
                                         timeout=timeout):
                    raise ProtocolError(f"no answer to request 0x{opcode:02x}")
                if self.errors:
                    raise ProtocolError(self.errors[0])
                ret, data = self._responses.pop(mid)
        finally:
            with self._cv:
                self._pending_requests.discard(mid)
                self._responses.pop(mid, None)
        if ret == MSG_REQUEST_DROPPED:
            raise ProtocolError(f"the board dropped request 0x{opcode:02x}")
        return ret, data

    def query_u32(self, opcode):
        _, data = self.request(opcode)
        return struct.unpack_from("<I", data)[0]

    def query_data(self, opcode):
        return self.request(opcode)[1]

    # -- command queue

    def command(self, opcode, body, capacity):
        """Queue one command on the board. Blocks while the queue is full.
        Raises Cancelled once cancel_commands() has run."""
        with self._cv:
            self._cv.wait_for(lambda: len(self.pending_commands) < capacity
                              or self.errors or self.cancelled)
            if self.errors:
                raise ProtocolError(self.errors[0])
        with self._wire:
            if self.cancelled:
                raise Cancelled()
            return self._send_command(opcode, body)

    def _send_command(self, opcode, body):
        with self._cv:
            mid = self._command_id
            self._command_id = (mid + 1) & 0xFF
            self.pending_commands[mid] = time.monotonic()
        self.link.send_stream(struct.pack("<HBB", 4 + len(body), opcode, mid) + body)
        return mid

    def wait_all(self, timeout):
        with self._cv:
            done = self._cv.wait_for(lambda: not self.pending_commands or self.errors,
                                     timeout=timeout)
            if self.errors:
                raise ProtocolError(self.errors[0])
            return done

    def cancel_commands(self):
        """Clear the board's queue (the current command stops at once). Later
        command() calls raise Cancelled."""
        with self._wire:
            with self._cv:
                self.cancelled = True
                self.pending_commands.clear()
                self._cv.notify_all()
            self.link.send_stream(struct.pack("<HBB", 4, MSG_CQ_CANCEL, 0))

    def release(self, opcode, body):
        """Queue one command even after a cancel: the buttons-up at a stop."""
        with self._wire:
            return self._send_command(opcode, body)


class Cancelled(Exception):
    pass


# ---- NS1 Pro Controller buttons ----------------------------------------------

# Byte (0 = right buttons, 1 = shared, 2 = left) and bit, from
# NintendoSwitch_PABotBase2_OemController.cpp populate_report_buttons().
BUTTON_BITS = {
    "Y": (0, 0x01), "X": (0, 0x02), "B": (0, 0x04), "A": (0, 0x08),
    "R": (0, 0x40), "ZR": (0, 0x80),
    "MINUS": (1, 0x01), "PLUS": (1, 0x02), "HOME": (1, 0x10), "CAPTURE": (1, 0x20),
    "DPAD_DOWN": (2, 0x01), "DPAD_UP": (2, 0x02), "DPAD_RIGHT": (2, 0x04),
    "DPAD_LEFT": (2, 0x08), "L": (2, 0x40), "ZL": (2, 0x80),
}
NEUTRAL_STICKS = bytes((0x00, 0x08, 0x80, 0x00, 0x08, 0x80))  # 12-bit 0x800 each


def buttons_body(buttons, milliseconds):
    """Body of a PABB2_MESSAGE_CMD_NS1_OEM_CONTROLLER_BUTTONS message."""
    if not 1 <= milliseconds <= 0xFFFF:
        raise ValueError(f"duration {milliseconds} ms is out of range")
    b = [0, 0, 0]
    for name in buttons:
        byte, bit = BUTTON_BITS[name]
        b[byte] |= bit
    return struct.pack("<H", milliseconds) + bytes(b) + NEUTRAL_STICKS + b"\x00"


def player_number(lights):
    """Switch player number from the player-light bits, or None."""
    lights = (lights | (lights >> 4)) & 0x0F
    return {0b0001: 1, 0b0011: 2, 0b0111: 3, 0b1111: 4,
            0b1001: 5, 0b0101: 6, 0b1101: 7, 0b0110: 8}.get(lights)
