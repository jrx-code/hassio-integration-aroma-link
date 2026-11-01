"""Aroma-Link Pro300 wire codec (reverse-engineered, see docs/protocol.md).

Pure stdlib. Encodes/decodes both the MQTT transport envelope and the inner
ASCII-hex frame. Checksum = XOR of every inner byte from AA(start) up to the
byte before the checksum.
"""
from __future__ import annotations

from dataclasses import dataclass

START = 0xAA
END = 0xEE
DIR_DOWN = 0xAC
DIR_UP = 0xCA
CMD_CONTROL = 0x57
CMD_QUERY = 0x52
SUB_POWER = 0x10
SUB_FAN = 0x11
SUB_SCHEDULE = 0x15
TAG_DOWN = 0x0023
TAG_UP = 0x0024


def _xor(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def mac_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def mac_from_inner(inner: bytes) -> str | None:
    """Extract the device MAC (AA:BB:CC:DD:EE:FF) from an inner frame."""
    if len(inner) < 9 or inner[0] != START:
        return None
    return ":".join(f"{b:02X}" for b in inner[3:9])


def topic_id(mac: str, prefix: str = "qdpu118T") -> str:
    return prefix + mac.replace(":", "").lower()


# ---------- inner frame ----------

def build_inner(mac: str, cmd: int, sub: int, payload: bytes,
                direction: int = DIR_DOWN, flag: int = 0x00) -> bytes:
    """Return the inner frame BYTES (not yet ASCII-hex-encoded)."""
    body = bytes([START, direction, flag]) + mac_bytes(mac) + bytes([0x00, cmd, sub]) + payload
    return body + bytes([_xor(body), END])


def switch_frame(mac: str, sub: int, on: bool) -> bytes:
    # observed control payload: 01 00 <val>
    return build_inner(mac, CMD_CONTROL, sub, bytes([0x01, 0x00, 0x01 if on else 0x00]))


def power_frame(mac: str, on: bool) -> bytes:
    return switch_frame(mac, SUB_POWER, on)


def fan_frame(mac: str, on: bool) -> bytes:
    return switch_frame(mac, SUB_FAN, on)


def query_frame(mac: str) -> bytes:
    # observed: 52 15 02 00 01 1F
    return build_inner(mac, CMD_QUERY, SUB_SCHEDULE, bytes([0x02, 0x00, 0x01, 0x1F]))


@dataclass
class Slot:
    """One schedule program (9 wire bytes)."""
    start_h: int = 0
    start_m: int = 0
    end_h: int = 23
    end_m: int = 59
    level: int = 1      # consistence level 1-3
    enabled: bool = False
    work_sec: int = 10
    pause_sec: int = 120

    def to_wire(self) -> bytes:
        flags = ((self.level & 0x0F) << 4) | (0x01 if self.enabled else 0x00)
        return (bytes([self.start_h, self.start_m, self.end_h, self.end_m, flags])
                + self.work_sec.to_bytes(2, "big") + self.pause_sec.to_bytes(2, "big"))

    @classmethod
    def from_wire(cls, s: bytes) -> Slot:
        return cls(s[0], s[1], s[2], s[3], s[4] >> 4, bool(s[4] & 0x0F),
                   int.from_bytes(s[5:7], "big"), int.from_bytes(s[7:9], "big"))


FILLER = Slot()  # 00:00-23:59, level 1, disabled, work 10, pause 120


def schedule_frame(mac: str, slots: list[Slot]) -> bytes:
    """workSet command (cmd 0x57 sub 0x15). Pads/truncates to exactly 5 slots.
    Header = <nslots*9+1> 00 01, then 5 x 9-byte slots.
    """
    sl = (list(slots) + [FILLER] * 5)[:5]
    body = b"".join(s.to_wire() for s in sl)
    header = bytes([len(body) + 1, 0x00, 0x01])
    return build_inner(mac, CMD_CONTROL, SUB_SCHEDULE, header + body)


def parse_schedule(payload: bytes) -> list[Slot]:
    """Parse the uplink/downlink schedule payload (after cmd+sub) into slots."""
    body = payload[3:]  # skip 3-byte header
    return [Slot.from_wire(body[i:i + 9]) for i in range(0, len(body) - 8, 9)]


CMD_STATE = 0x53   # uplink that carries actual state (not the 0x57 command-ack)


def interpret_uplink(inner: bytes):
    """Map an uplink inner frame to a semantic event.

    Observed (captured from the real device):
      0x53 / 0x11  -> actual FAN state in the last payload byte (01/00)
      0x57 / 10|11 -> command ACK (last byte 0x00, NOT the state) -> ('ack', sub)
      0x52|0x53 / 0x15 -> schedule/status dump -> ('schedule', [Slot,...])
    Power STATE is not echoed as a dedicated 0x53/0x10 frame in the captures;
    treat power optimistically on command and reconcile from the schedule/status
    dump at cutover (TODO: confirm the on/off field in the 0x52 response live).
    """
    d = parse_inner(inner)
    if not d or d.direction != DIR_UP:
        return None
    if d.cmd == CMD_STATE and d.sub == SUB_FAN:
        return ("fan", bool(d.payload[-1]))
    if d.cmd == CMD_CONTROL and d.sub in (SUB_POWER, SUB_FAN):
        return ("ack", d.sub)
    if d.cmd in (CMD_QUERY, CMD_STATE) and d.sub == SUB_SCHEDULE:
        return ("schedule", parse_schedule(d.payload))
    return None


@dataclass
class Decoded:
    direction: int
    cmd: int
    sub: int
    payload: bytes
    valid: bool


def parse_inner(frame: bytes) -> Decoded | None:
    if len(frame) < 6 or frame[0] != START or frame[-1] != END:
        return None
    ck = frame[-2]
    body = frame[:-2]
    valid = _xor(body) == ck
    direction = frame[1]
    # layout: AA dir flag MAC(6) 00 cmd sub payload...
    cmd = frame[10] if len(frame) > 10 else None
    sub = frame[11] if len(frame) > 11 else None
    payload = frame[12:-2]
    return Decoded(direction, cmd, sub, payload, valid)


# ---------- MQTT transport envelope ----------

def envelope_checksum(msgid: int, tag: int, ascii_hex: bytes) -> int:
    """8-bit additive checksum over msgid(2) + tag(2) + ASCII-hex payload.

    Verified against 28/28 captured cloud frames. The byte after the length
    field is THIS checksum, not a sequence counter; a frame with a wrong value
    is PUBACKed by the device's MQTT stack but silently dropped by its
    application layer.
    """
    return (sum(msgid.to_bytes(2, "big")) + sum(tag.to_bytes(2, "big")) + sum(ascii_hex)) & 0xFF


class Envelope:
    """Builds AA AA | len16 | cksum8 | msgid16 | tag16 | <ascii-hex> payloads.

    msgid must not repeat a value the device has already seen (replay is
    dropped), so it starts high and increases monotonically.
    """

    def __init__(self, start_msgid: int = 0x1000) -> None:
        self._msgid = start_msgid & 0xFFFF

    def wrap(self, inner: bytes, tag: int = TAG_DOWN) -> bytes:
        self._msgid = (self._msgid + 1) & 0xFFFF
        ascii_hex = inner.hex().upper().encode("ascii")
        body = self._msgid.to_bytes(2, "big") + tag.to_bytes(2, "big") + ascii_hex
        rest = bytes([envelope_checksum(self._msgid, tag, ascii_hex)]) + body
        return bytes([START, START]) + len(rest).to_bytes(2, "big") + rest


def verify_envelope(payload: bytes) -> bool:
    """True when an AA AA envelope carries a valid checksum byte."""
    if len(payload) < 9 or payload[:2] != bytes([START, START]):
        return False
    msgid = int.from_bytes(payload[5:7], "big")
    tag = int.from_bytes(payload[7:9], "big")
    return payload[4] == envelope_checksum(msgid, tag, payload[9:])


def unwrap(payload: bytes) -> bytes | None:
    """Extract the inner frame bytes from an MQTT payload (strip envelope)."""
    idx = payload.find(b"AA", 5)  # ASCII 'A''A' start of the hex frame
    if idx < 0:
        return None
    ascii_hex = payload[idx:]
    # trim to the final 'EE' terminator of the hex string
    end = ascii_hex.rfind(b"EE")
    if end < 0:
        return None
    try:
        return bytes.fromhex(ascii_hex[:end + 2].decode("ascii"))
    except ValueError:
        return None


if __name__ == "__main__":
    # Consistency self-test with a documentation-only example MAC. The
    # byte-exact match against real captured device frames lives in the
    # project's private test suite, not in the distributed component.
    MAC = "AA:BB:CC:DD:EE:FF"
    for frame in (power_frame(MAC, True), power_frame(MAC, False),
                  fan_frame(MAC, True), fan_frame(MAC, False), query_frame(MAC),
                  schedule_frame(MAC, [Slot(0, 0, 23, 59, 2, True, 7, 123)])):
        d = parse_inner(frame)
        assert d is not None and d.valid, frame.hex()
        assert mac_from_inner(frame) == MAC
    # fan state round-trips through an uplink echo frame
    up = build_inner(MAC, CMD_STATE, SUB_FAN, bytes([0x01, 0x00, 0x01]),
                     direction=DIR_UP, flag=0x01)
    assert interpret_uplink(up) == ("fan", True)
    # schedule encode -> parse -> re-encode is stable
    slots = [Slot(0, 0, 23, 59, 2, True, 7, 123)]
    sched = schedule_frame(MAC, slots)
    d = parse_inner(sched)
    assert parse_schedule(d.payload)[0] == Slot(0, 0, 23, 59, 2, True, 7, 123)
    print("codec self-test OK (encode/parse/checksum/uplink/schedule consistent)")
