"""An in-memory stand-in for the PABotBase2 board, for tests.

Speaks the board side of the protocol (reset, version, packet size, the
reliable stream both ways, requests and the command queue). Commands run on a
virtual clock, so a test needs no real waits. It can drop or damage packets in
both directions to exercise retransmits and reassembly.
"""

import queue
import struct
import threading
import time

from ace_typer import pabb2 as p


class FakeBoard:
    def __init__(self, capacity=255, mode=p.CID_NS1_WIRED_PRO_CONTROLLER,
                 flags=0x06, lights=0x01, drop_in=0, drop_out=0, garbage=False,
                 realtime=0.0):
        self.capacity = capacity
        self.mode = mode
        self.flags = flags
        self.lights = lights
        self.drop_in = drop_in      # drop every Nth packet from the host
        self.drop_out = drop_out    # drop every Nth packet to the host
        self.garbage = garbage      # noise bytes before packets to the host
        self.realtime = realtime    # real seconds per command millisecond
        self.cancelled_at = []      # len(commands) at each cancel
        self.events = []            # ("cmd", report bytes, ms) / ("cancel",) / ("replace",)
        self._replace = False
        self._interrupts = 0        # a cancel or a replace ends the running command
        self.session = 0
        self.parser = p.PacketParser()
        self.in_seq = 0
        self.in_offset = 0
        self.msg_buf = bytearray()
        self.out_seq = 0
        self.out_offset = 0
        self.unacked = {}           # seq -> packet bytes
        self.commands = []          # (button bytes, ms) run, in order
        self.cancels = 0
        self.clock_us = 1_000_000
        self.serial = FakeSerial(self)
        self._queue = []
        self._lock = threading.RLock()
        self._n_in = 0
        self._n_out = 0
        self._stop = False
        self._work = threading.Condition(self._lock)
        threading.Thread(target=self._executor, daemon=True).start()
        threading.Thread(target=self._retransmitter, daemon=True).start()

    # -- wire

    def _to_host(self, data, droppable=True):
        self._n_out += 1
        if droppable and self.drop_out and self._n_out % self.drop_out == 0:
            return
        if self.garbage:
            data = b"\x81\x00\x03\xffnoise" + data
        self.serial.rx.put(data)

    def from_host(self, data):
        with self._lock:
            for seq, opcode, payload in self.parser.feed(data, self.session):
                self._n_in += 1
                if self.drop_in and self._n_in % self.drop_in == 0:
                    continue
                self._on_packet(seq, opcode & p.OPCODE_MASK, payload)

    def _reply(self, seq, opcode, payload=b""):
        self._to_host(p.encode_packet(self.session, seq, opcode, payload))

    def _on_packet(self, seq, op, payload):
        if op == p.ASK_RESET:
            self.session = struct.unpack("<I", payload)[0]
            self.in_seq, self.in_offset = 1, 0
            self.out_seq, self.out_offset = 0, 0
            self.msg_buf.clear()
            self.unacked.clear()
            self._queue.clear()
            self._reply(seq, p.RET_RESET)
        elif op == p.ASK_VERSION:
            self._take_slot(seq)
            self._reply(seq, p.RET_VERSION, struct.pack("<I", p.CONNECTION_PROTOCOL))
        elif op == p.ASK_PACKET_SIZE:
            self._take_slot(seq)
            self._reply(seq, p.RET_PACKET_SIZE, struct.pack("<I", 256))
        elif op == p.ASK_STREAM_DATA:
            ahead = (seq - self.in_seq) & 0xFF
            if ahead >= 128:        # duplicate: the host missed our ack
                self._reply(seq, p.RET_STREAM_DATA, struct.pack("<I", 16384))
                return
            if ahead != 0:
                return
            offset = struct.unpack_from("<H", payload)[0]
            assert offset == self.in_offset, (offset, self.in_offset)
            data = payload[2:]
            self.in_seq = (self.in_seq + 1) & 0xFF
            self.in_offset = (self.in_offset + len(data)) & 0xFFFF
            self._reply(seq, p.RET_STREAM_DATA, struct.pack("<I", 16384))
            self.msg_buf += data
            self._read_messages()
        elif op == p.RET_STREAM_DATA:
            self.unacked.pop(seq, None)

    def _take_slot(self, seq):
        if seq == self.in_seq:
            self.in_seq = (self.in_seq + 1) & 0xFF

    def _send_message(self, opcode, mid, body=b""):
        msg = struct.pack("<HBB", 4 + len(body), opcode, mid) + body
        seq = self.out_seq
        pkt = p.encode_packet(self.session, seq, p.ASK_STREAM_DATA,
                              struct.pack("<H", self.out_offset) + msg)
        self.out_seq = (seq + 1) & 0xFF
        self.out_offset = (self.out_offset + len(msg)) & 0xFFFF
        self.unacked[seq] = pkt
        self._to_host(pkt)

    def _retransmitter(self):
        while not self._stop:
            time.sleep(0.02)
            with self._lock:
                for pkt in list(self.unacked.values())[:1]:
                    self._to_host(pkt, droppable=False)

    # -- messages

    def _read_messages(self):
        while len(self.msg_buf) >= 4:
            size, opcode, mid = struct.unpack_from("<HBB", self.msg_buf)
            if len(self.msg_buf) < size:
                return
            body = bytes(self.msg_buf[4:size])
            del self.msg_buf[:size]
            self._on_message(opcode, mid, body)

    def _on_message(self, opcode, mid, body):
        u32 = {p.MSG_PROTOCOL_VERSION: p.MESSAGE_PROTOCOL,
               p.MSG_FIRMWARE_VERSION: 2026090200,
               p.MSG_CQ_CAPACITY: self.capacity,
               p.MSG_READ_CONTROLLER_MODE: self.mode}
        if opcode in u32:
            self._send_message(p.MSG_RET_U32, mid, struct.pack("<I", u32[opcode]))
        elif opcode == p.MSG_DEVICE_NAME:
            self._send_message(p.MSG_RET_DATA, mid, b"PABotBase2-ESP32-S3")
        elif opcode == p.MSG_REQUEST_STATUS:
            self._send_message(p.MSG_RET_U32_DATA, mid,
                               struct.pack("<IBB", self.mode, self.flags, self.lights) + bytes(6))
        elif opcode == p.MSG_NS1_BUTTONS:
            if self._replace:
                self._replace = False
                self._queue.clear()
                self._interrupts += 1
            elif len(self._queue) >= self.capacity:
                self._send_message(p.MSG_CQ_COMMAND_DROPPED, mid)
                return
            ms = struct.unpack_from("<H", body)[0]
            self._queue.append((mid, bytes(body[2:5]), ms, bytes(body[2:11])))
            self._work.notify_all()
        elif opcode == p.MSG_CQ_CANCEL:
            self._queue.clear()
            self.cancels += 1
            self._interrupts += 1
            self.cancelled_at.append(len(self.commands))
            self.events.append(("cancel",))
            self._work.notify_all()
        elif opcode == p.MSG_CQ_REPLACE_ON_NEXT:
            self._replace = True
            self.events.append(("replace",))

    def _executor(self):
        while not self._stop:
            with self._lock:
                self._work.wait_for(lambda: self._queue or self._stop, timeout=0.1)
                if not self._queue:
                    continue
                mid, buttons, ms, report = self._queue.pop(0)
                self.commands.append((buttons, ms))
                self.events.append(("cmd", report, ms))
                # Hold for the command's time; a cancel or replace ends it at once.
                interrupts = self._interrupts
                self._work.wait_for(lambda: self._interrupts != interrupts or self._stop,
                                    timeout=max(0.0005, ms * self.realtime))
                if self._interrupts != interrupts:
                    continue
                self.clock_us += ms * 1000
                self._send_message(p.MSG_CQ_COMMAND_FINISHED, mid,
                                   struct.pack("<I", self.clock_us & 0xFFFFFFFF))

    def close(self):
        self._stop = True


class FakeSerial:
    """Host end of the fake wire: the pyserial calls Link uses."""

    def __init__(self, board):
        self.board = board
        self.rx = queue.Queue()
        self.baudrate = 921600
        self.closed = False

    def read(self, n):
        try:
            return self.rx.get(timeout=0.01)
        except queue.Empty:
            return b""

    def write(self, data):
        self.board.from_host(bytes(data))
        return len(data)

    def close(self):
        self.closed = True
        self.board.close()
