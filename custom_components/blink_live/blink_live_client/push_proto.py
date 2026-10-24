"""Small bounded protobuf/MCS codec; wire definitions adapted from pyfcm-receiver.

See NOTICE for attribution. Only Android check-in and MCS fields are implemented.
"""

import asyncio

MAX_FRAME = 1024 * 1024


def varint(value: int) -> bytes:
    if not 0 <= value < 2**64:
        raise ValueError("Invalid protobuf integer")
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def number(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def blob(field: int, value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return varint((field << 3) | 2) + varint(len(data)) + data


def fields(data: bytes) -> dict[int, list[int | bytes]]:
    """Decode supported wire types, rejecting truncation and oversized integers."""
    if len(data) > MAX_FRAME:
        raise ValueError("Oversized protobuf")
    pos = 0

    def read_varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError("Truncated protobuf")
            byte = data[pos]
            pos += 1
            if shift == 63 and byte > 1:
                raise ValueError("Invalid protobuf integer")
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("Invalid protobuf integer")

    result = {}
    while pos < len(data):
        key = read_varint()
        field, wire = key >> 3, key & 7
        if not 0 < field < 2**29:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value = read_varint()
        elif wire in (1, 2, 5):
            size = read_varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - pos:
                raise ValueError("Truncated protobuf field")
            chunk = data[pos : pos + size]
            pos += size
            value = chunk if wire == 2 else int.from_bytes(chunk, "little")
        else:
            raise ValueError("Unsupported protobuf wire type")
        result.setdefault(field, []).append(value)
    return result


def integer(data: dict, field: int, default: int = 0) -> int:
    value = data.get(field, [default])[-1]
    if not isinstance(value, int):
        raise ValueError("Invalid protobuf number")
    return value


def string(data: dict, field: int, default: str = "") -> str:
    value = data.get(field, [default.encode()])[-1]
    if not isinstance(value, bytes):
        raise ValueError("Invalid protobuf string")
    return value.decode("utf-8")


def checkin_request(state: dict) -> bytes:
    data = blob(4, number(12, 1)) + blob(6, "en_US") + blob(12, "GMT") + number(14, 3)
    if state.get("android_id") and state.get("security_token"):
        data += number(2, int(state["android_id"]))
        data += varint((13 << 3) | 1) + int(state["security_token"]).to_bytes(8, "little")
    return data


def login_request(state: dict) -> bytes:
    android_id = int(state["android_id"])
    data = b"".join(
        blob(field, value)
        for field, value in (
            (1, "chrome-63.0.3234.0"),
            (2, "mcs.android.com"),
            (3, str(android_id)),
            (4, str(android_id)),
            (5, str(state["security_token"])),
            (6, f"android-{android_id:x}"),
        )
    )
    data += blob(8, blob(1, "new_vc") + blob(2, "1"))
    data += b"".join(blob(10, pid) for pid in state.get("persistent_ids", [])[-1000:])
    return data + number(12, 0) + number(14, 1) + number(16, 2) + number(17, 1)


def frame(tag: int, data: bytes = b"") -> bytes:
    return bytes([tag]) + varint(len(data)) + data


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    tag = (await reader.readexactly(1))[0]
    size = 0
    for shift in range(0, 35, 7):
        byte = (await reader.readexactly(1))[0]
        size |= (byte & 127) << shift
        if size > MAX_FRAME:
            raise ValueError("Oversized MCS frame")
        if byte < 128:
            return tag, await reader.readexactly(size)
    raise ValueError("Invalid MCS frame length")


def stream_ack(stream_id: int) -> bytes:
    data = number(2, 1) + blob(3, "") + blob(7, number(1, 13) + blob(2, b""))
    return data + number(10, stream_id) + number(12, 0)
