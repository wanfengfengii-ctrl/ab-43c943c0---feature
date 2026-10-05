"""Strict Standard MIDI File (SMF) parsing and timeline normalization.

Only formats 0 and 1 with a positive PPQN division are supported.  The
parser is deliberately strict: truncated data, malformed variable-length
integers, broken running status, illegal status bytes, bad event lengths
and undeclared trailing bytes are all rejected with a byte offset that
locates the problem.  No partial timeline is ever produced: either the
whole file parses and every channel event gets an exact microsecond
instant (a reduced fraction), or a single :class:`MidiError` is raised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Tuple

MAX_FILE_BYTES = 1 << 20  # 1 MiB
MAX_TRACKS_PLUS_EVENTS = 10_000
DEFAULT_TEMPO_US_PER_QUARTER = 500_000

CHANNEL_EVENT_NAMES = {
    0x80: "note_off",
    0x90: "note_on",
    0xA0: "polyphonic_key_pressure",
    0xB0: "control_change",
    0xC0: "program_change",
    0xD0: "channel_pressure",
    0xE0: "pitch_bend",
}

_META_TEMPO = 0x51
_META_END_OF_TRACK = 0x2F


class MidiError(Exception):
    """A structural MIDI problem located at an exact byte offset."""

    def __init__(self, code: str, message: str, offset: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset


@dataclass
class ChannelEvent:
    tick: int
    track: int
    order: int  # ordinal among the channel events of this track
    kind: int  # high nibble of the status byte, e.g. 0x90
    channel: int
    data: Tuple[int, ...]


@dataclass
class TempoEvent:
    tick: int
    tempo_us: int


@dataclass
class ParsedMidi:
    fmt: int
    ppqn: int
    ntracks: int
    channel_events: List[ChannelEvent] = field(default_factory=list)
    tempo_events: List[TempoEvent] = field(default_factory=list)  # track 0 only


def _read_varlen(data: bytes, pos: int, end: int) -> Tuple[int, int]:
    """Read a 1-4 byte variable-length integer from ``data[pos:end]``."""
    start = pos
    value = 0
    for _ in range(4):
        if pos >= end:
            raise MidiError(
                "truncated_varlen",
                f"unterminated variable-length integer starting at offset {start}",
                start,
            )
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, pos
    raise MidiError(
        "varlen_too_long",
        f"variable-length integer at offset {start} exceeds 4 bytes",
        start,
    )


def parse(data: bytes) -> ParsedMidi:
    """Parse and strictly validate a whole SMF file."""
    if len(data) > MAX_FILE_BYTES:
        raise MidiError(
            "file_too_large",
            f"file is {len(data)} bytes, limit is {MAX_FILE_BYTES}",
            MAX_FILE_BYTES,
        )
    if len(data) < 14:
        raise MidiError(
            "truncated_header",
            f"file is {len(data)} bytes, a header chunk needs at least 14",
            len(data),
        )
    if data[0:4] != b"MThd":
        raise MidiError("bad_magic", "missing 'MThd' header magic", 0)
    header_len = int.from_bytes(data[4:8], "big")
    if header_len != 6:
        raise MidiError(
            "bad_header_length",
            f"header chunk length must be 6, got {header_len}",
            4,
        )
    fmt = int.from_bytes(data[8:10], "big")
    if fmt not in (0, 1):
        raise MidiError(
            "unsupported_format",
            f"only SMF formats 0 and 1 are supported, got {fmt}",
            8,
        )
    ntracks = int.from_bytes(data[10:12], "big")
    division = int.from_bytes(data[12:14], "big")
    if division & 0x8000:
        raise MidiError(
            "unsupported_division",
            "SMPTE time division is not supported; a positive PPQN is required",
            12,
        )
    if division == 0:
        raise MidiError("invalid_division", "PPQN division must be positive", 12)
    if ntracks == 0:
        raise MidiError("no_tracks", "file declares zero tracks", 10)
    if fmt == 0 and ntracks != 1:
        raise MidiError(
            "bad_track_count",
            f"format 0 must contain exactly one track, got {ntracks}",
            10,
        )
    if ntracks > MAX_TRACKS_PLUS_EVENTS:
        raise MidiError(
            "limit_exceeded",
            f"track count {ntracks} exceeds the limit of {MAX_TRACKS_PLUS_EVENTS} "
            "for tracks plus channel events",
            10,
        )

    parsed = ParsedMidi(fmt=fmt, ppqn=division, ntracks=ntracks)
    pos = 8 + header_len
    for track_index in range(ntracks):
        if pos + 8 > len(data):
            raise MidiError(
                "truncated_track_header",
                f"track {track_index}: missing 8-byte MTrk chunk header at offset {pos}",
                pos,
            )
        if data[pos : pos + 4] != b"MTrk":
            raise MidiError(
                "bad_chunk",
                f"track {track_index}: expected 'MTrk' chunk at offset {pos}, "
                f"found {data[pos:pos + 4]!r}",
                pos,
            )
        track_len = int.from_bytes(data[pos + 4 : pos + 8], "big")
        track_start = pos + 8
        track_end = track_start + track_len
        if track_end > len(data):
            raise MidiError(
                "truncated_track",
                f"track {track_index}: declared length {track_len} at offset "
                f"{pos + 4} exceeds the {len(data) - track_start} remaining bytes",
                pos + 4,
            )
        _parse_track(data, track_start, track_end, track_index, parsed)
        pos = track_end
    if pos != len(data):
        raise MidiError(
            "trailing_bytes",
            f"{len(data) - pos} undeclared trailing byte(s) after the last track",
            pos,
        )
    return parsed


def _parse_track(
    data: bytes, start: int, end: int, track_index: int, parsed: ParsedMidi
) -> None:
    pos = start
    tick = 0
    running_status = None
    order = 0
    while pos < end:
        delta, pos = _read_varlen(data, pos, end)
        tick += delta
        if pos >= end:
            raise MidiError(
                "truncated_event",
                f"track {track_index}: event at tick {tick} is missing its "
                f"status byte at offset {pos}",
                pos,
            )
        status_offset = pos
        byte = data[pos]
        if byte >= 0x80:
            status = byte
            pos += 1
            # System-exclusive and meta events cancel running status.
            running_status = status if status < 0xF0 else None
        else:
            if running_status is None:
                raise MidiError(
                    "orphan_running_status",
                    f"track {track_index}: data byte 0x{byte:02X} at offset {pos} "
                    "has no running status",
                    pos,
                )
            status = running_status

        if status < 0xF0:
            kind = status & 0xF0
            needed = 1 if kind in (0xC0, 0xD0) else 2
            if pos + needed > end:
                raise MidiError(
                    "truncated_event",
                    f"track {track_index}: channel event at offset {status_offset} "
                    f"needs {needed} data byte(s), only {end - pos} remain",
                    pos,
                )
            payload = data[pos : pos + needed]
            for i, value in enumerate(payload):
                if value >= 0x80:
                    raise MidiError(
                        "invalid_data_byte",
                        f"track {track_index}: data byte 0x{value:02X} at offset "
                        f"{pos + i} has the high bit set",
                        pos + i,
                    )
            pos += needed
            if parsed.ntracks + len(parsed.channel_events) + 1 > MAX_TRACKS_PLUS_EVENTS:
                raise MidiError(
                    "limit_exceeded",
                    f"tracks plus channel events exceed the limit of "
                    f"{MAX_TRACKS_PLUS_EVENTS}",
                    status_offset,
                )
            parsed.channel_events.append(
                ChannelEvent(
                    tick=tick,
                    track=track_index,
                    order=order,
                    kind=kind,
                    channel=status & 0x0F,
                    data=tuple(payload),
                )
            )
            order += 1
        elif status == 0xFF:
            if pos >= end:
                raise MidiError(
                    "truncated_event",
                    f"track {track_index}: meta event at offset {status_offset} "
                    "is missing its type byte",
                    pos,
                )
            meta_type = data[pos]
            pos += 1
            meta_len, pos = _read_varlen(data, pos, end)
            if pos + meta_len > end:
                raise MidiError(
                    "truncated_event",
                    f"track {track_index}: meta event 0x{meta_type:02X} at offset "
                    f"{status_offset} declares {meta_len} byte(s), only "
                    f"{end - pos} remain",
                    pos,
                )
            payload = data[pos : pos + meta_len]
            pos += meta_len
            if meta_type == _META_TEMPO:
                if meta_len != 3:
                    raise MidiError(
                        "bad_meta_length",
                        f"track {track_index}: tempo meta event at offset "
                        f"{status_offset} must be 3 bytes, got {meta_len}",
                        status_offset,
                    )
                tempo_us = int.from_bytes(payload, "big")
                if tempo_us == 0:
                    raise MidiError(
                        "invalid_tempo",
                        f"track {track_index}: tempo at offset {status_offset} "
                        "must be positive",
                        status_offset,
                    )
                # Only the first track builds the global tempo map.  For
                # format 0 that is also the only track, so the rule is uniform.
                if track_index == 0:
                    parsed.tempo_events.append(TempoEvent(tick=tick, tempo_us=tempo_us))
            elif meta_type == _META_END_OF_TRACK and meta_len != 0:
                raise MidiError(
                    "bad_meta_length",
                    f"track {track_index}: end-of-track meta event at offset "
                    f"{status_offset} must have length 0, got {meta_len}",
                    status_offset,
                )
        elif status in (0xF0, 0xF7):
            sysex_len, pos = _read_varlen(data, pos, end)
            if pos + sysex_len > end:
                raise MidiError(
                    "truncated_event",
                    f"track {track_index}: sysex event at offset {status_offset} "
                    f"declares {sysex_len} byte(s), only {end - pos} remain",
                    pos,
                )
            pos += sysex_len
        else:
            raise MidiError(
                "illegal_status",
                f"track {track_index}: status byte 0x{status:02X} at offset "
                f"{status_offset} is not allowed in a MIDI file",
                status_offset,
            )


def build_tempo_segments(tempo_events: List[TempoEvent]) -> List[Tuple[int, int]]:
    """Build (start_tick, tempo_us) segments from track-0 tempo events.

    A tempo event takes effect at its own tick; when several tempo events
    share a tick the last one wins.  The default is 500000 us per quarter.
    """
    segments: List[Tuple[int, int]] = [(0, DEFAULT_TEMPO_US_PER_QUARTER)]
    for event in tempo_events:  # ticks are non-decreasing within a track
        if event.tick < segments[-1][0]:
            continue  # defensive; cannot happen for cumulative parsing
        if event.tick == segments[-1][0]:
            segments[-1] = (event.tick, event.tempo_us)
        else:
            segments.append((event.tick, event.tempo_us))
    return segments


def time_at_tick(
    tick: int, segments: List[Tuple[int, int]], ppqn: int
) -> Fraction:
    """Exact time in microseconds of ``tick`` under the tempo segments."""
    total = Fraction(0, 1)
    for i, (seg_start, tempo_us) in enumerate(segments):
        if tick <= seg_start:
            break
        seg_end = segments[i + 1][0] if i + 1 < len(segments) else None
        stop = tick if seg_end is None else min(tick, seg_end)
        total += Fraction((stop - seg_start) * tempo_us, ppqn)
        if seg_end is None or tick <= seg_end:
            break
    return total


def normalize(data: bytes) -> dict:
    """Parse ``data`` and return the normalized channel-event timeline."""
    parsed = parse(data)
    segments = build_tempo_segments(parsed.tempo_events)
    ordered = sorted(
        parsed.channel_events, key=lambda e: (e.tick, e.track, e.order)
    )
    events = []
    for event in ordered:
        moment = time_at_tick(event.tick, segments, parsed.ppqn)
        events.append(
            {
                "tick": event.tick,
                "track": event.track,
                "order": event.order,
                "type": CHANNEL_EVENT_NAMES[event.kind],
                "channel": event.channel,
                "data": list(event.data),
                "time_us": {
                    "numerator": moment.numerator,
                    "denominator": moment.denominator,
                    "fraction": f"{moment.numerator}/{moment.denominator}",
                },
            }
        )
    return {
        "format": parsed.fmt,
        "ppqn": parsed.ppqn,
        "track_count": parsed.ntracks,
        "channel_event_count": len(events),
        "events": events,
    }
