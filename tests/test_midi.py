"""Unit tests for the strict SMF parser and timeline normalizer."""

import unittest
from fractions import Fraction

from app.midi import (
    AUDIBLE_NOTES_PROJECTION,
    MAX_TRACKS_PLUS_EVENTS,
    MidiError,
    ProjectionError,
    build_tempo_segments,
    normalize,
    parse,
    time_at_tick,
    TempoEvent,
)


# -- SMF builders ---------------------------------------------------------


def varlen(n):
    out = bytes([n & 0x7F])
    n >>= 7
    while n:
        out = bytes([(n & 0x7F) | 0x80]) + out
        n >>= 7
    return out


def track(payload):
    return b"MTrk" + len(payload).to_bytes(4, "big") + payload


def header(fmt, ntracks, division):
    return (
        b"MThd"
        + (6).to_bytes(4, "big")
        + fmt.to_bytes(2, "big")
        + ntracks.to_bytes(2, "big")
        + division.to_bytes(2, "big")
    )


def tempo(delta, us):
    return varlen(delta) + bytes([0xFF, 0x51, 0x03]) + us.to_bytes(3, "big")


def ev(delta, *bs):
    return varlen(delta) + bytes(bs)


def note_on(delta, ch, pitch, vel):
    return ev(delta, 0x90 | ch, pitch, vel)


def note_off(delta, ch, pitch, vel=0):
    return ev(delta, 0x80 | ch, pitch, vel)


def cc(delta, ch, controller, value):
    return ev(delta, 0xB0 | ch, controller, value)


SUSTAIN = 64


EOT = ev(0, 0xFF, 0x2F, 0x00)


def fractions(result):
    return [Fraction(e["time_us"]["numerator"], e["time_us"]["denominator"])
            for e in result["events"]]


# -- header / chunk validation --------------------------------------------


class HeaderTests(unittest.TestCase):
    def test_bad_magic(self):
        with self.assertRaises(MidiError) as ctx:
            parse(b"NOPE" + bytes(10))
        self.assertEqual(ctx.exception.code, "bad_magic")
        self.assertEqual(ctx.exception.offset, 0)

    def test_truncated_header(self):
        with self.assertRaises(MidiError) as ctx:
            parse(b"MThd\x00\x00")
        self.assertEqual(ctx.exception.code, "truncated_header")
        self.assertEqual(ctx.exception.offset, 6)

    def test_bad_header_length(self):
        data = b"MThd" + (8).to_bytes(4, "big") + bytes(8)
        with self.assertRaises(MidiError) as ctx:
            parse(data)
        self.assertEqual(ctx.exception.code, "bad_header_length")
        self.assertEqual(ctx.exception.offset, 4)

    def test_format_2_rejected(self):
        with self.assertRaises(MidiError) as ctx:
            parse(header(2, 2, 480))
        self.assertEqual(ctx.exception.code, "unsupported_format")
        self.assertEqual(ctx.exception.offset, 8)

    def test_smpte_division_rejected(self):
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 0xE728))  # -25 fps, 40 ticks/frame
        self.assertEqual(ctx.exception.code, "unsupported_division")
        self.assertEqual(ctx.exception.offset, 12)

    def test_zero_ppqn_rejected(self):
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 0))
        self.assertEqual(ctx.exception.code, "invalid_division")

    def test_zero_tracks_rejected(self):
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 0, 480))
        self.assertEqual(ctx.exception.code, "no_tracks")

    def test_format_0_requires_single_track(self):
        with self.assertRaises(MidiError) as ctx:
            parse(header(0, 2, 480))
        self.assertEqual(ctx.exception.code, "bad_track_count")

    def test_missing_track_chunk(self):
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480))
        self.assertEqual(ctx.exception.code, "truncated_track_header")
        self.assertEqual(ctx.exception.offset, 14)

    def test_wrong_chunk_type(self):
        data = header(1, 1, 480) + b"LIST" + bytes(4)
        with self.assertRaises(MidiError) as ctx:
            parse(data)
        self.assertEqual(ctx.exception.code, "bad_chunk")
        self.assertEqual(ctx.exception.offset, 14)

    def test_truncated_track_body(self):
        data = header(1, 1, 480) + b"MTrk" + (100).to_bytes(4, "big") + b"\x00\xff"
        with self.assertRaises(MidiError) as ctx:
            parse(data)
        self.assertEqual(ctx.exception.code, "truncated_track")
        self.assertEqual(ctx.exception.offset, 18)

    def test_trailing_bytes_rejected(self):
        data = header(1, 1, 480) + track(EOT) + b"\x00"
        with self.assertRaises(MidiError) as ctx:
            parse(data)
        self.assertEqual(ctx.exception.code, "trailing_bytes")
        self.assertEqual(ctx.exception.offset, len(data) - 1)

    def test_file_too_large(self):
        with self.assertRaises(MidiError) as ctx:
            parse(b"\x00" * (1 << 20 | 1))
        self.assertEqual(ctx.exception.code, "file_too_large")


# -- event-level validation ------------------------------------------------


class EventTests(unittest.TestCase):
    def test_varlen_too_long(self):
        payload = b"\x80\x80\x80\x80\x00" + ev(0, 0x90, 60, 100)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "varlen_too_long")
        self.assertEqual(ctx.exception.offset, 22)

    def test_unterminated_varlen(self):
        payload = b"\x80\x80"
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "truncated_varlen")
        self.assertEqual(ctx.exception.offset, 22)

    def test_missing_status_byte(self):
        payload = ev(0, 0x90, 60, 100) + varlen(10)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "truncated_event")

    def test_orphan_running_status(self):
        payload = ev(0, 60, 100)  # data bytes before any status
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "orphan_running_status")
        self.assertEqual(ctx.exception.offset, 23)

    def test_running_status_cancelled_by_meta(self):
        payload = ev(0, 0x90, 60, 100) + ev(0, 0xFF, 0x01, 0x00) + ev(0, 62, 100)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "orphan_running_status")

    def test_running_status_cancelled_by_sysex(self):
        payload = ev(0, 0x90, 60, 100) + ev(0, 0xF0, 0x00) + ev(0, 62, 100)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "orphan_running_status")

    def test_running_status_ok(self):
        payload = ev(0, 0x90, 60, 100) + ev(10, 62, 100) + EOT
        result = normalize(header(1, 1, 480) + track(payload))
        self.assertEqual(result["channel_event_count"], 2)
        self.assertEqual(result["events"][1]["data"], [62, 100])
        self.assertEqual(result["events"][1]["tick"], 10)

    def test_illegal_status(self):
        payload = ev(0, 0xF8)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "illegal_status")
        self.assertEqual(ctx.exception.offset, 23)

    def test_data_byte_high_bit(self):
        payload = ev(0, 0x90, 60, 0x80)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "invalid_data_byte")
        self.assertEqual(ctx.exception.offset, 25)

    def test_truncated_channel_event(self):
        payload = ev(0, 0x90, 60)  # note on needs two data bytes
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "truncated_event")

    def test_program_change_single_data_byte(self):
        payload = ev(0, 0xC3, 5) + ev(0, 0xD0, 7) + EOT
        result = normalize(header(1, 1, 480) + track(payload))
        self.assertEqual(
            [e["type"] for e in result["events"]],
            ["program_change", "channel_pressure"],
        )
        self.assertEqual(result["events"][0]["channel"], 3)

    def test_truncated_meta_event(self):
        payload = ev(0, 0xFF, 0x01, 0x05) + b"ab"  # declares 5, gives 2
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "truncated_event")

    def test_bad_tempo_length(self):
        payload = ev(0, 0xFF, 0x51, 0x02, 0x07, 0xA1)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "bad_meta_length")

    def test_zero_tempo_rejected(self):
        payload = tempo(0, 0)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "invalid_tempo")

    def test_bad_end_of_track_length(self):
        payload = ev(0, 0xFF, 0x2F, 0x01, 0x00)
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "bad_meta_length")

    def test_truncated_sysex(self):
        payload = ev(0, 0xF0, 0x05) + b"ab"
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "truncated_event")

    def test_sysex_and_meta_accepted(self):
        payload = (
            ev(0, 0xF0, 0x03, 0x7E, 0x7F, 0x09)
            + ev(0, 0xFF, 0x03, 0x04) + b"name"
            + ev(0, 0x90, 60, 100)
            + EOT
        )
        result = normalize(header(1, 1, 480) + track(payload))
        self.assertEqual(result["channel_event_count"], 1)


# -- limits ------------------------------------------------------------------


class LimitTests(unittest.TestCase):
    def test_track_and_event_total_limit(self):
        # 1 track + 9999 channel events == 10000: accepted.
        payload = ev(0, 0x90, 60, 100) + ev(0, 60, 100) * 9998 + EOT
        result = normalize(header(1, 1, 480) + track(payload))
        self.assertEqual(result["channel_event_count"], 9999)

        # One more channel event crosses the limit.
        payload = ev(0, 0x90, 60, 100) + ev(0, 60, 100) * 9999 + EOT
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, 1, 480) + track(payload))
        self.assertEqual(ctx.exception.code, "limit_exceeded")

    def test_track_count_limit(self):
        with self.assertRaises(MidiError) as ctx:
            parse(header(1, MAX_TRACKS_PLUS_EVENTS + 1, 480))
        self.assertEqual(ctx.exception.code, "limit_exceeded")
        self.assertEqual(ctx.exception.offset, 10)


# -- tempo map and timing ----------------------------------------------------


class TempoTests(unittest.TestCase):
    def test_default_tempo(self):
        payload = ev(0, 0x90, 60, 100) + ev(480, 0x80, 60, 0) + EOT
        result = normalize(header(1, 1, 480) + track(payload))
        self.assertEqual(fractions(result), [Fraction(0), Fraction(500000)])

    def test_reduced_fraction(self):
        payload = ev(1, 0x90, 60, 100) + EOT
        result = normalize(header(1, 1, 480) + track(payload))
        self.assertEqual(fractions(result), [Fraction(3125, 3)])
        self.assertEqual(result["events"][0]["time_us"]["fraction"], "3125/3")

    def test_tempo_takes_effect_at_its_tick(self):
        conductor = tempo(0, 500000) + tempo(480, 250000) + EOT
        notes = ev(480, 0x90, 60, 100) + ev(480, 0x80, 60, 0) + EOT
        data = header(1, 2, 480) + track(conductor) + track(notes)
        result = normalize(data)
        # Tick 480 itself is still priced with the old tempo; the new tempo
        # only governs the interval that starts at tick 480.
        self.assertEqual(
            fractions(result), [Fraction(500000), Fraction(750000)]
        )

    def test_same_tick_last_tempo_wins(self):
        conductor = tempo(0, 600000) + tempo(0, 250000) + EOT
        notes = ev(480, 0x90, 60, 100) + EOT
        data = header(1, 2, 480) + track(conductor) + track(notes)
        result = normalize(data)
        self.assertEqual(fractions(result), [Fraction(250000)])

    def test_format_1_ignores_non_first_track_tempos(self):
        conductor = tempo(0, 500000) + EOT
        notes = tempo(0, 1000) + ev(480, 0x90, 60, 100) + EOT
        data = header(1, 2, 480) + track(conductor) + track(notes)
        result = normalize(data)
        self.assertEqual(fractions(result), [Fraction(500000)])

    def test_format_0_uses_own_tempos(self):
        payload = tempo(0, 250000) + ev(480, 0x90, 60, 100) + EOT
        result = normalize(header(0, 1, 480) + track(payload))
        self.assertEqual(result["format"], 0)
        self.assertEqual(fractions(result), [Fraction(250000)])

    def test_multi_segment_tempo_map(self):
        segments = build_tempo_segments(
            [TempoEvent(0, 500000), TempoEvent(480, 250000), TempoEvent(960, 1000000)]
        )
        self.assertEqual(time_at_tick(0, segments, 480), Fraction(0))
        self.assertEqual(time_at_tick(240, segments, 480), Fraction(250000))
        self.assertEqual(time_at_tick(480, segments, 480), Fraction(500000))
        self.assertEqual(time_at_tick(960, segments, 480), Fraction(750000))
        self.assertEqual(time_at_tick(1200, segments, 480), Fraction(1250000))

    def test_tempo_beyond_last_event_is_harmless(self):
        conductor = tempo(0, 500000) + tempo(9999, 250000) + EOT
        notes = ev(480, 0x90, 60, 100) + EOT
        data = header(1, 2, 480) + track(conductor) + track(notes)
        result = normalize(data)
        self.assertEqual(fractions(result), [Fraction(500000)])


# -- ordering ----------------------------------------------------------------


class OrderingTests(unittest.TestCase):
    def test_stable_order_by_tick_track_order(self):
        t0 = ev(240, 0x90, 60, 100) + EOT
        t1 = ev(0, 0x91, 61, 100) + ev(240, 0x81, 61, 0) + EOT
        t2 = ev(120, 0x92, 62, 100) + ev(120, 0x82, 62, 0) + EOT
        data = header(1, 3, 480) + track(t0) + track(t1) + track(t2)
        result = normalize(data)
        keys = [(e["tick"], e["track"], e["order"]) for e in result["events"]]
        self.assertEqual(
            keys,
            [(0, 1, 0), (120, 2, 0), (240, 0, 0), (240, 1, 1), (240, 2, 1)],
        )
        self.assertEqual(
            fractions(result),
            [Fraction(0)] + [Fraction(125000)] + [Fraction(250000)] * 3,
        )


# -- audible notes projection ------------------------------------------------


def note_tuple(note):
    """Compact (ch, pitch, vel, start_t, release_t, end_t) for assertions."""
    return (
        note["channel"],
        note["pitch"],
        note["velocity"],
        note["start"]["tick"],
        note["release"]["tick"],
        note["end"]["tick"],
    )


class AudibleNotesTests(unittest.TestCase):
    def _normalize(self, payload, ntracks=1, fmt=1, ppqn=480):
        tracks = payload if isinstance(payload, list) else [payload]
        data = header(fmt, ntracks, ppqn) + b"".join(
            track(p) for p in tracks
        )
        return normalize(data, projection=AUDIBLE_NOTES_PROJECTION)

    def test_no_projection_keeps_response_compatible(self):
        payload = note_on(0, 0, 60, 100) + note_off(10, 0, 60) + EOT
        result = normalize(header(1, 1, 480) + track(payload))
        self.assertNotIn("notes", result)
        self.assertIn("events", result)

    def test_explicit_none_projection_keeps_response_compatible(self):
        payload = note_on(0, 0, 60, 100) + note_off(10, 0, 60) + EOT
        result = normalize(header(1, 1, 480) + track(payload), projection=None)
        self.assertNotIn("notes", result)

    def test_simple_note_off(self):
        payload = (
            note_on(0, 0, 60, 110)
            + note_off(480, 0, 60)
            + EOT
        )
        result = self._normalize(payload)
        notes = result["notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(note_tuple(notes[0]), (0, 60, 110, 0, 480, 480))
        # Events are still present and ordered as before.
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(
            [(e["tick"], e["track"], e["order"]) for e in result["events"]],
            [(0, 0, 0), (480, 0, 1)],
        )

    def test_zero_velocity_note_on_releases(self):
        payload = (
            note_on(0, 0, 60, 90)
            + note_on(480, 0, 60, 0)  # zero-velocity note on == release
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            note_tuple(result["notes"][0]), (0, 60, 90, 0, 480, 480)
        )

    def test_note_off_velocity_is_ignored(self):
        payload = (
            note_on(0, 0, 60, 90)
            + note_off(480, 0, 60, vel=64)
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            note_tuple(result["notes"][0]), (0, 60, 90, 0, 480, 480)
        )

    def test_notes_sorted_by_attack_order(self):
        # All attacks share tick 0; track order decides attack order even
        # though pitches are out of order across tracks.
        t0 = note_on(0, 0, 70, 100) + note_off(0, 0, 70) + EOT
        t1 = note_on(0, 1, 60, 100) + note_off(0, 1, 60) + EOT
        t2 = note_on(0, 2, 64, 100) + note_off(0, 2, 64) + EOT
        result = self._normalize([t0, t1, t2], ntracks=3)
        self.assertEqual(
            [(n["channel"], n["pitch"]) for n in result["notes"]],
            [(0, 70), (1, 60), (2, 64)],
        )

    def test_notes_attack_order_across_ticks(self):
        # Track index is lower but the attack lands later: tick dominates.
        t0 = note_on(240, 0, 60, 100) + note_off(0, 0, 60) + EOT
        t1 = note_on(0, 1, 64, 100) + note_off(240, 1, 64) + EOT
        result = self._normalize([t0, t1], ntracks=2)
        self.assertEqual(
            [(n["channel"], n["pitch"]) for n in result["notes"]],
            [(1, 64), (0, 60)],
        )

    def test_cross_track_same_tick_attack_then_release(self):
        # Attack and release on the same channel/pitch land in different
        # tracks at the same tick; track order makes attack come first.
        attacks = note_on(0, 0, 60, 100) + EOT
        releases = note_off(0, 0, 60) + EOT
        result = self._normalize([attacks, releases], ntracks=2)
        self.assertEqual(
            note_tuple(result["notes"][0]), (0, 60, 100, 0, 0, 0)
        )

    def test_cross_track_same_tick_release_first_fails(self):
        # Track 0 releases before track 1 attacks at the same tick.
        releases = note_off(0, 0, 60) + EOT
        attacks = note_on(0, 0, 60, 100) + EOT
        with self.assertRaises(ProjectionError) as ctx:
            self._normalize([releases, attacks], ntracks=2)
        self.assertEqual(ctx.exception.code, "unmatched_note_off")

    def test_sustain_pedal_extends_end(self):
        payload = (
            cc(0, 0, SUSTAIN, 64)       # pedal down
            + note_on(0, 0, 60, 100)
            + note_off(240, 0, 60)      # key released at 240
            + cc(240, 0, SUSTAIN, 0)    # pedal up at 480: audible end
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            note_tuple(result["notes"][0]), (0, 60, 100, 0, 240, 480)
        )

    def test_sustain_threshold_boundary(self):
        # Value 63 is pedal up; 64 is down.
        payload = (
            cc(0, 0, SUSTAIN, 63)
            + note_on(0, 0, 60, 100)
            + note_off(240, 0, 60)
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            note_tuple(result["notes"][0]), (0, 60, 100, 0, 240, 240)
        )

    def test_pedal_stays_down_repeated_cc_does_not_end(self):
        payload = (
            cc(0, 0, SUSTAIN, 100)
            + note_on(0, 0, 60, 100)
            + note_off(100, 0, 60)
            + cc(100, 0, SUSTAIN, 80)   # still down
            + cc(100, 0, SUSTAIN, 127)  # still down
            + cc(100, 0, SUSTAIN, 63)   # up: ends at tick 400
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            note_tuple(result["notes"][0]), (0, 60, 100, 0, 100, 400)
        )

    def test_pedal_up_ends_all_sustained_on_channel(self):
        payload = (
            cc(0, 0, SUSTAIN, 127)
            + note_on(0, 0, 60, 100)
            + note_on(0, 0, 64, 90)
            + note_off(120, 0, 60)
            + note_off(120, 0, 64)
            + cc(120, 0, SUSTAIN, 0)
            + cc(0, 1, SUSTAIN, 127)    # other channel: pedal down but no note
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            sorted(note_tuple(n) for n in result["notes"]),
            [
                (0, 60, 100, 0, 120, 360),
                (0, 64, 90, 0, 240, 360),
            ],
        )

    def test_sustain_per_channel_isolation(self):
        # Channel 1's pedal-down CC must not suspend a channel 0 release.
        payload = (
            cc(0, 1, SUSTAIN, 127)
            + note_on(0, 0, 60, 100)
            + note_off(100, 0, 60)
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            note_tuple(result["notes"][0]), (0, 60, 100, 0, 100, 100)
        )

    def test_sustain_left_down_on_other_channel_fails(self):
        payload = (
            cc(0, 1, SUSTAIN, 127)
            + note_on(0, 1, 60, 100)
            + note_off(100, 1, 60)
            + EOT
        )
        with self.assertRaises(ProjectionError) as ctx:
            self._normalize(payload)
        self.assertEqual(ctx.exception.code, "unterminated_notes")

    def test_reattack_same_pitch_while_sustained(self):
        payload = (
            cc(0, 0, SUSTAIN, 127)
            + note_on(0, 0, 60, 100)
            + note_off(240, 0, 60)      # parked by pedal
            + note_on(0, 0, 60, 80)     # re-attack allowed while sustained
            + note_off(240, 0, 60)
            + cc(240, 0, SUSTAIN, 0)    # both end at 720
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            [note_tuple(n) for n in result["notes"]],
            [
                (0, 60, 100, 0, 240, 720),
                (0, 60, 80, 240, 480, 720),
            ],
        )

    def test_repeated_note_on_without_release_fails(self):
        payload = (
            note_on(0, 0, 60, 100)
            + note_on(240, 0, 60, 90)
            + note_off(0, 0, 60)
            + EOT
        )
        with self.assertRaises(ProjectionError) as ctx:
            self._normalize(payload)
        self.assertEqual(ctx.exception.code, "repeated_note_on")

    def test_unmatched_note_off_fails(self):
        payload = note_off(0, 0, 60) + EOT
        with self.assertRaises(ProjectionError) as ctx:
            self._normalize(payload)
        self.assertEqual(ctx.exception.code, "unmatched_note_off")

    def test_unmatched_zero_velocity_note_on_fails(self):
        payload = note_on(0, 0, 60, 0) + EOT
        with self.assertRaises(ProjectionError) as ctx:
            self._normalize(payload)
        self.assertEqual(ctx.exception.code, "unmatched_note_off")

    def test_unterminated_held_note_fails(self):
        payload = note_on(0, 0, 60, 100) + EOT  # never released
        with self.assertRaises(ProjectionError) as ctx:
            self._normalize(payload)
        self.assertEqual(ctx.exception.code, "unterminated_notes")

    def test_unterminated_sustained_note_fails(self):
        payload = (
            cc(0, 0, SUSTAIN, 127)
            + note_on(0, 0, 60, 100)
            + note_off(100, 0, 60)
            + EOT  # pedal never comes up
        )
        with self.assertRaises(ProjectionError) as ctx:
            self._normalize(payload)
        self.assertEqual(ctx.exception.code, "unterminated_notes")

    def test_non_sustain_cc_ignored(self):
        payload = (
            cc(0, 0, 7, 127)           # volume, not sustain
            + note_on(0, 0, 60, 100)
            + note_off(100, 0, 60)
            + EOT
        )
        result = self._normalize(payload)
        self.assertEqual(
            note_tuple(result["notes"][0]), (0, 60, 100, 0, 100, 100)
        )

    def test_other_channel_events_do_not_match(self):
        payload = (
            note_on(0, 0, 60, 100)
            + note_off(0, 1, 60)       # different channel -> unmatched
            + note_off(0, 0, 60)
            + EOT
        )
        with self.assertRaises(ProjectionError) as ctx:
            self._normalize(payload)
        self.assertEqual(ctx.exception.code, "unmatched_note_off")

    def test_tempo_changes_apply_to_note_fractions(self):
        conductor = (
            tempo(0, 500000)
            + tempo(480, 250000)
            + EOT
        )
        notes = (
            cc(0, 0, SUSTAIN, 127)
            + note_on(0, 0, 60, 100)   # tick 0
            + note_off(480, 0, 60)     # release tick 480
            + cc(480, 0, SUSTAIN, 0)   # end tick 960
            + EOT
        )
        result = self._normalize([conductor, notes], ntracks=2)
        n = result["notes"][0]
        self.assertEqual(n["start"]["time_us"]["fraction"], "0/1")
        self.assertEqual(n["release"]["time_us"]["fraction"], "500000/1")
        # 480 ticks @ 500000 -> 500000 us; 480 ticks @ 250000 -> 250000 us.
        self.assertEqual(n["end"]["time_us"]["fraction"], "750000/1")

    def test_fractional_microseconds_reduced(self):
        # ppqn 480, one tick = 500000/480 = 3125/3 us.
        payload = (
            note_on(0, 0, 60, 100)
            + note_off(1, 0, 60)
            + EOT
        )
        result = self._normalize(payload)
        n = result["notes"][0]
        self.assertEqual(n["start"]["time_us"]["fraction"], "0/1")
        self.assertEqual(n["release"]["time_us"]["fraction"], "3125/3")
        self.assertEqual(n["end"]["time_us"]["fraction"], "3125/3")

    def test_note_payload_fields(self):
        payload = note_on(0, 3, 72, 127) + note_off(10, 3, 72) + EOT
        result = self._normalize(payload)
        n = result["notes"][0]
        self.assertEqual(n["channel"], 3)
        self.assertEqual(n["pitch"], 72)
        self.assertEqual(n["velocity"], 127)
        for key in ("start", "release", "end"):
            self.assertIn("tick", n[key])
            self.assertEqual(
                set(n[key]["time_us"]),
                {"numerator", "denominator", "fraction"},
            )


if __name__ == "__main__":
    unittest.main()
