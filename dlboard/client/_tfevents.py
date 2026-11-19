"""
TensorBoard event files, read without TensorFlow or the `tensorboard` package (#702).

An event file is TFRecord framing around `Event` protos: each record is its length (8 bytes, little-endian), a checksum
of that (4), the bytes, and a checksum of them (4). The few messages an import needs (`Event`, `Summary`, an image, a
histogram, a tensor, the hparams plugin's start of a session) are decoded straight from the protobuf wire format here, by
field number, so a client install needs nothing more to read them. Checksums are not verified: a record cut short (a run
killed mid-write) is noticed by its length, and reading stops before it, at the last whole record.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, BinaryIO, TypeAlias, Union

from dlboard import models

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_HEADER = struct.Struct("<QI")
_FOOTER = 4


class _Wire(IntEnum):
    VARINT = 0
    I64 = 1
    LEN = 2
    I32 = 5


_Field: TypeAlias = "tuple[int, int | bytes]"


def _varint(data: bytes, at: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7


def _fields(data: bytes) -> Iterator[_Field]:
    """Each field of a message, as its number and its raw value: an int for a varint or a fixed width, bytes for the rest."""
    at = 0
    while at < len(data):
        key, at = _varint(data, at)
        number, wire = key >> 3, _Wire(key & 0x7)
        match wire:
            case _Wire.VARINT:
                value, at = _varint(data, at)
                yield number, value
            case _Wire.I64:
                yield number, data[at : at + 8]
                at += 8
            case _Wire.I32:
                yield number, data[at : at + 4]
                at += 4
            case _Wire.LEN:
                length, at = _varint(data, at)
                yield number, data[at : at + length]
                at += length


def _double(raw: int | bytes) -> float:
    return struct.unpack("<d", raw)[0] if isinstance(raw, bytes) else float(raw)


def _float(raw: int | bytes) -> float:
    return struct.unpack("<f", raw)[0] if isinstance(raw, bytes) else float(raw)


def _bytes(raw: int | bytes) -> bytes:
    return raw if isinstance(raw, bytes) else b""


def _doubles(raws: list[int | bytes]) -> list[float]:
    """A repeated double, packed (one run of bytes) or not (eight bytes a field): either way, every eight bytes is one."""
    return [value for raw in raws for value in struct.unpack(f"<{len(_bytes(raw)) // 8}d", _bytes(raw))]


def _floats(raws: list[int | bytes]) -> list[float]:
    """A repeated float, as `_doubles` reads a repeated double."""
    return [value for raw in raws for value in struct.unpack(f"<{len(_bytes(raw)) // 4}f", _bytes(raw))]


@dataclass(frozen=True)
class Scalar:
    tag: str
    value: float


@dataclass(frozen=True)
class Histo:
    tag: str
    histogram: models.Histogram


@dataclass(frozen=True)
class Picture:
    tag: str
    png: bytes


@dataclass(frozen=True)
class Hparams:
    values: models.FlatHparamDict


@dataclass(frozen=True)
class Unread:
    """A summary of a kind an import does not bring across (text, audio, PR curves, ...), by its plugin's name."""

    plugin: str


Summary: TypeAlias = Union[Scalar, Histo, Picture, Hparams, Unread]


@dataclass(frozen=True)
class Event:
    step: int
    wall_time: float
    summaries: list[Summary] = field(default_factory=list)


def records(path: Path, offset: int) -> Iterator[tuple[int, bytes]]:
    """Each whole record of the file from byte `offset` on, with the offset just past it: stops at one cut short."""
    with path.open("rb") as file:
        file.seek(offset)
        while (record := _record(file)) is not None:
            offset += _HEADER.size + len(record) + _FOOTER
            yield offset, record


def _record(file: BinaryIO) -> bytes | None:
    header = file.read(_HEADER.size)
    if len(header) < _HEADER.size:
        return None
    length, _ = _HEADER.unpack(header)
    data = file.read(length)
    footer = file.read(_FOOTER)
    return data if len(data) == length and len(footer) == _FOOTER else None


def event(record: bytes) -> Event:
    """An `Event` record's step, time and the summaries it holds (none, for the file's version stamp)."""
    step, wall_time, summaries = 0, 0.0, list[Summary]()
    for number, raw in _fields(record):
        match number:
            case 1:
                wall_time = _double(raw)
            case 2:
                step = raw if isinstance(raw, int) else 0
            case 5:
                summaries.extend(
                    summary
                    for n, value in _fields(_bytes(raw))
                    if n == 1
                    if (summary := _value(_bytes(value)))
                )
            case _:
                pass  # the file's version, a graph, a log message, a session's status: nothing an import keeps
    return Event(step=step, wall_time=wall_time, summaries=summaries)


@dataclass
class _Value:
    tag: str = ""
    simple: float | None = None
    image: bytes | None = None
    histo: bytes | None = None
    tensor: bytes | None = None
    plugin: str = ""
    content: bytes = b""


def _value(data: bytes) -> Summary | None:
    value = _Value()
    for number, raw in _fields(data):
        match number:
            case 1:
                value.tag = _bytes(raw).decode("utf-8", "replace")
            case 2:
                value.simple = _float(raw)
            case 4:
                value.image = _bytes(raw)
            case 5:
                value.histo = _bytes(raw)
            case 8:
                value.tensor = _bytes(raw)
            case 9:
                value.plugin, value.content = _plugin(_bytes(raw))
            case _:
                pass  # a node name, audio, a histogram of the oldest style: nothing an import keeps
    return _summary(value)


def _plugin(metadata: bytes) -> tuple[str, bytes]:
    for number, raw in _fields(metadata):
        if number == 1:
            fields = dict(_fields(_bytes(raw)))
            return _bytes(fields.get(1, b"")).decode(), _bytes(fields.get(2, b""))
    return "", b""


def _summary(value: _Value) -> Summary | None:  # noqa: PLR0911 -- one answer per kind of summary
    if value.simple is not None:
        return Scalar(value.tag, value.simple)
    if value.image is not None:
        return Picture(value.tag, _bytes(dict(_fields(value.image)).get(4, b"")))
    if value.histo is not None:
        histogram = _histogram_of_proto(value.histo)
        return None if histogram is None else Histo(value.tag, histogram)
    match value.plugin:
        case "hparams":
            hparams = _hparams(value.content)
            return None if hparams is None else Hparams(hparams)
        case "scalars" if value.tensor is not None:
            numbers = _tensor_numbers(value.tensor)
            return Scalar(value.tag, numbers[0]) if numbers else None
        case "histograms" if value.tensor is not None:
            histogram = _histogram_of_triples(_tensor_numbers(value.tensor))
            return None if histogram is None else Histo(value.tag, histogram)
        case "images" if value.tensor is not None:
            # Its width, its height, then each image: the first is the one kept.
            images = _tensor_strings(value.tensor)[2:]
            return Picture(value.tag, images[0]) if images else None
        case "":
            return None
        case other:
            return Unread(other)


_DT_FLOAT, _DT_DOUBLE = 1, 2


def _tensor_numbers(tensor: bytes) -> list[float]:
    """A float or double tensor's values, from its packed content or its `float_val`/`double_val`."""
    dtype, content, floats, doubles = 0, b"", list[int | bytes](), list[int | bytes]()
    for number, raw in _fields(tensor):
        match number:
            case 1:
                dtype = raw if isinstance(raw, int) else 0
            case 4:
                content = _bytes(raw)
            case 5:
                floats.append(raw)
            case 6:
                doubles.append(raw)
            case _:
                pass
    if content and dtype == _DT_FLOAT:
        return list(struct.unpack(f"<{len(content) // 4}f", content))
    if content and dtype == _DT_DOUBLE:
        return list(struct.unpack(f"<{len(content) // 8}d", content))
    return _floats(floats) + _doubles(doubles)


def _tensor_strings(tensor: bytes) -> list[bytes]:
    return [_bytes(raw) for number, raw in _fields(tensor) if number == 8]  # noqa: PLR2004 -- `string_val`


def _histogram_of_proto(histo: bytes) -> models.Histogram | None:
    """A `HistogramProto`: bucket `i` holds `bucket[i]` values up to `bucket_limit[i]`, from the one before (or `min`)."""
    low = high = 0.0
    limits, counts = list[int | bytes](), list[int | bytes]()
    for number, raw in _fields(histo):
        match number:
            case 1:
                low = _double(raw)
            case 2:
                high = _double(raw)
            case 6:
                limits.append(raw)
            case 7:
                counts.append(raw)
            case _:
                pass  # how many, their sum and the sum of their squares: a dlboard histogram is its buckets
    rights = _doubles(limits)
    lefts = [low, *rights[:-1]]
    return _histogram(list(zip(lefts, rights, _doubles(counts), strict=False)), low, high)


def _histogram_of_triples(numbers: list[float]) -> models.Histogram | None:
    """The histograms plugin's tensor: a `(left, right, count)` for each bucket."""
    buckets = [(numbers[i], numbers[i + 1], numbers[i + 2]) for i in range(0, len(numbers) - 2, 3)]
    if not buckets:
        return None
    return _histogram(buckets, buckets[0][0], buckets[-1][1])


def _histogram(buckets: list[tuple[float, float, float]], low: float, high: float) -> models.Histogram | None:
    """
    `buckets` (`(left, right, count)`) as a dlboard histogram, or `None` if they hold nothing.

    The empty ones at either end are dropped, every edge is kept within what was seen (TensorBoard's outermost edges are
    the largest doubles there are), and a bucket that leaves no width is merged into the one before it.
    """
    filled = [i for i, (_, _, count) in enumerate(buckets) if count > 0]
    if not filled or not (math.isfinite(low) and math.isfinite(high)):
        return None
    if high <= low:  # every value the same: one bucket around it
        return models.Histogram(edges=[low - 0.5, low + 0.5], counts=[round(sum(c for _, _, c in buckets))])
    edges, counts = [min(max(buckets[filled[0]][0], low), high)], list[int]()
    for _, right, count in buckets[filled[0] : filled[-1] + 1]:
        edge = min(max(right, low), high)
        if edge > edges[-1]:
            edges.append(edge)
            counts.append(round(count))
        elif counts:
            counts[-1] += round(count)
    return models.Histogram(edges=edges, counts=counts) if counts else None


def _hparams(content: bytes) -> models.FlatHparamDict | None:
    """The hparams plugin's start of a session (`HParamsPluginData.session_start_info.hparams`): its values by name."""
    start = next((_bytes(raw) for number, raw in _fields(content) if number == 3), None)  # noqa: PLR2004
    if start is None:
        return None
    values: models.FlatHparamDict = {}
    for number, raw in _fields(start):
        if number != 1:
            continue
        entry = dict(_fields(_bytes(raw)))
        name = _bytes(entry.get(1, b"")).decode()
        for kind, inner in _fields(_bytes(entry.get(2, b""))):
            match kind:
                case 2:
                    values[name] = _double(inner)
                case 3:
                    values[name] = _bytes(inner).decode()
                case 4:
                    values[name] = bool(inner)
                case _:
                    pass  # null, a struct, a list: not a hyperparameter value dlboard keeps
    return values
