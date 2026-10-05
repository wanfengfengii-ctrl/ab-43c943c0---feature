"""Unit tests for the audible-notes projection."""

import unittest
from fractions import Fraction

from app.midi import (
    AUDIBLE_NOTES_PROJECTION,
    ProjectionError,
    normalize,
    project_audible_notes,
)


# -- SMF builders (mirrors the ones in test_midi) ---------------------------


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


def note_on(delta, channel, pitch, velocity):
    return ev(delta, 0x90 | channel, pitch, velocity)


def note_off(delta, channel, pitch, velocity=0):
    return ev(delta, 0x80 | channel, pitch, velocity)


def pedal(delta, channel, value):
    return ev(delta, 0xB0 | channel, 64, value)


EOT = ev(0, 0xFF, 0x2F, 0x00)


def project(payload, fmt=1, ntracks=1, ppqn=480, tracks=None):
    if tracks is None:
        tracks = [payload]
    data = header(fmt, ntracks, ppqn) + b"".join(track(t) for t in tracks)
    result = normalize(data, projection=AUDIBLE_NOTES_PROJECTION)
    return result["notes"]


def frac(blob):
    return Fraction(blob["numerator"], blob["denominator"])


# -- happy paths -------------------------------------------------------------


class BasicNoteTests(unittest.TestCase):
    def test_plain_note_release_and_end_coincide(self):
        payload = (
            note_on(0, 0, 60, 100)
            + note_off(480, 0, 60)
            + EOT
        )
        notes = project(payload)
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note["channel"], 0)
        self.assertEqual(note["pitch"], 60)
        self.assertEqual(note["velocity"], 100)
        self.assertEqual(
            (note["start_tick"], note["release_tick"], note["end_tick"]),
            (0, 480, 480),
        )
        self.assertEqual(frac(note["start_us"]), Fraction(0))
        self.assertEqual(frac(note["release_us"]), Fraction(500000))
        self.assertEqual(frac(note["end_us"]), Fraction(500000))
        # Fractions are reported in reduced form.
        self.assertEqual(note["release_us"]["fraction"], "500000/1")

    def test_zero_velocity_note_on_is_a_release(self):
        payload = (
            note_on(0, 1, 62, 80)
            + ev(240, 0x91, 62, 0)  # note on, velocity 0 == release
            + EOT
        )
        notes = project(payload)
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            (notes[0]["release_tick"], notes[0]["end_tick"]), (240, 240)
        )

    def test_notes_omitted_without_projection(self):
        payload = note_on(0, 0, 60, 100) + note_off(10, 0, 60) + EOT
        data = header(1, 1, 480) + track(payload)
        result = normalize(data)
        self.assertNotIn("notes", result)
        self.assertEqual(result["channel_event_count"], 2)
        # Explicit projection keeps the full event timeline.
        projected = normalize(data, projection=AUDIBLE_NOTES_PROJECTION)
        self.assertEqual(len(projected["events"]), 2)
        self.assertEqual(len(projected["notes"]), 1)

    def test_empty_file_gives_empty_notes(self):
        self.assertEqual(project(tempo(0, 500000) + EOT), [])


class PedalTests(unittest.TestCase):
    def test_pedal_holds_release_until_lift(self):
        payload = (
            pedal(0, 0, 64)                       # tick 0   pedal down
            + note_on(0, 0, 60, 100)              # tick 0   attack
            + note_off(240, 0, 60)                # tick 240 key released
            + pedal(480, 0, 63)                   # tick 720 pedal up
            + EOT
        )
        notes = project(payload)
        note = notes[0]
        self.assertEqual(
            (note["start_tick"], note["release_tick"], note["end_tick"]),
            (0, 240, 720),
        )
        self.assertEqual(frac(note["release_us"]), Fraction(250000))
        self.assertEqual(frac(note["end_us"]), Fraction(750000))

    def test_threshold_value_63_lifts_64_holds(self):
        # Value 64 holds; 63 lifts.  A second low value must not matter.
        payload = (
            pedal(0, 0, 64)
            + note_on(0, 0, 60, 90)
            + note_off(100, 0, 60)
            + pedal(100, 0, 100)                   # stays down
            + pedal(100, 0, 63)                    # tick 300: lift
            + pedal(100, 0, 0)                     # already up, no-op
            + EOT
        )
        notes = project(payload)
        self.assertEqual((notes[0]["release_tick"], notes[0]["end_tick"]),
                         (100, 300))

    def test_pedal_lift_ends_all_sustaining_notes_on_channel(self):
        payload = (
            pedal(0, 0, 127)
            + note_on(0, 0, 60, 100)
            + note_off(10, 0, 60)                  # sustained
            + note_on(10, 0, 64, 110)
            + note_off(10, 0, 64)                  # sustained
            + pedal(100, 0, 0)                     # both end here
            + EOT
        )
        notes = project(payload)
        self.assertEqual(len(notes), 2)
        for note in notes:
            self.assertEqual(note["end_tick"], 130)
        self.assertEqual([n["pitch"] for n in notes], [60, 64])

    def test_pedal_state_is_per_channel(self):
        payload = (
            pedal(0, 0, 127)                       # ch0 pedal down
            + note_on(0, 1, 60, 100)               # ch1, pedal up
            + note_off(100, 1, 60)                 # ends immediately
            + note_on(0, 0, 72, 90)                # ch0
            + note_off(100, 0, 72)                 # sustained on ch0
            + pedal(100, 1, 0)                     # ch1 lift: ch0 unaffected
            + pedal(100, 0, 0)                     # ch0 lift
            + EOT
        )
        notes = sorted(project(payload), key=lambda n: n["channel"])
        ch0, ch1 = notes
        self.assertEqual((ch1["release_tick"], ch1["end_tick"]), (100, 100))
        self.assertEqual((ch0["release_tick"], ch0["end_tick"]), (200, 400))

    def test_reattack_while_sustaining_is_independent(self):
        payload = (
            pedal(0, 0, 127)
            + note_on(0, 0, 60, 100)               # N1
            + note_off(100, 0, 60)                 # N1 sustained
            + note_on(100, 0, 60, 80)              # N2 re-attack, allowed
            + note_off(100, 0, 60)                 # N2 released, sustained
            + pedal(100, 0, 0)                     # both end at tick 400
            + EOT
        )
        notes = project(payload)
        self.assertEqual(len(notes), 2)
        self.assertEqual([n["velocity"] for n in notes], [100, 80])
        self.assertEqual(
            [(n["start_tick"], n["release_tick"], n["end_tick"]) for n in notes],
            [(0, 100, 400), (200, 300, 400)],
        )

    def test_release_with_pedal_up_then_pedal_down_does_not_revive(self):
        payload = (
            note_on(0, 0, 60, 100)
            + note_off(100, 0, 60)                 # ends at 100
            + pedal(100, 0, 127)                   # late pedal: no effect
            + pedal(100, 0, 0)
            + EOT
        )
        notes = project(payload)
        self.assertEqual(notes[0]["end_tick"], 100)


class OrderingAndTempoTests(unittest.TestCase):
    def test_cross_track_same_tick_uses_track_order(self):
        # Attack on track 0 and release on track 1 at the identical tick:
        # track 0 is processed first, so the note is complete at that tick.
        t0 = note_on(120, 0, 60, 100) + EOT
        t1 = note_off(120, 0, 60) + EOT
        notes = project(None, ntracks=2, tracks=[t0, t1])
        self.assertEqual(
            (notes[0]["start_tick"], notes[0]["release_tick"]), (120, 120)
        )

    def test_notes_sorted_by_attack_order(self):
        t0 = note_on(200, 2, 70, 100) + note_off(10, 2, 70) + EOT
        t1 = note_on(50, 0, 60, 100) + note_off(10, 0, 60) + EOT
        t2 = note_on(100, 1, 64, 100) + note_off(10, 1, 64) + EOT
        notes = project(None, ntracks=3, tracks=[t0, t1, t2])
        self.assertEqual(
            [(n["start_tick"], n["channel"]) for n in notes],
            [(50, 0), (100, 1), (200, 2)],
        )

    def test_tempo_change_between_attack_release_and_end(self):
        payload = (
            tempo(0, 500000)
            + pedal(0, 0, 127)
            + note_on(0, 0, 60, 100)               # tick 0,   0 us
            + tempo(480, 250000)                   # tick 480 tempo change
            + note_off(480, 0, 60)                 # tick 960 release
            + tempo(0, 1000000)                    # tick 960, last wins edge
            + pedal(480, 0, 0)                     # tick 1440 end
            + EOT
        )
        notes = project(payload)
        note = notes[0]
        self.assertEqual(frac(note["start_us"]), Fraction(0))
        # 0..480 @500000 = 500000; 480..960 @250000 = 250000 -> 750000
        self.assertEqual(frac(note["release_us"]), Fraction(750000))
        # 960..1440 @1000000 = 1000000 -> 1750000
        self.assertEqual(frac(note["end_us"]), Fraction(1750000))

    def test_fractional_microseconds_reduced(self):
        # ppqn 480, default tempo: tick 1 -> 3125/3 us.
        payload = (
            note_on(0, 0, 60, 100)
            + note_off(1, 0, 60)
            + EOT
        )
        notes = project(payload)
        self.assertEqual(notes[0]["release_us"]["fraction"], "3125/3")


# -- failure semantics (HTTP 422 at the API layer) --------------------------


class ProjectionFailureTests(unittest.TestCase):
    def _project_events(self, payload):
        data = header(1, 1, 480) + track(payload)
        return normalize(data, projection=AUDIBLE_NOTES_PROJECTION)

    def test_repeated_attack_without_release(self):
        payload = (
            note_on(0, 0, 60, 100)
            + note_on(100, 0, 60, 100)
            + note_off(10, 0, 60)
            + EOT
        )
        with self.assertRaises(ProjectionError) as ctx:
            self._project_events(payload)
        self.assertEqual(ctx.exception.code, "note_on_without_off")
        self.assertEqual((ctx.exception.tick, ctx.exception.track), (100, 0))

    def test_repeated_attack_without_release_even_with_pedal(self):
        # Pedal does not make an unreleased key re-attackable: the key never
        # went up, so this is still a 422.
        payload = (
            pedal(0, 0, 127)
            + note_on(0, 0, 60, 100)
            + note_on(100, 0, 60, 100)
            + EOT
        )
        with self.assertRaises(ProjectionError) as ctx:
            self._project_events(payload)
        self.assertEqual(ctx.exception.code, "note_on_without_off")

    def test_release_without_matching_key(self):
        payload = note_off(0, 0, 60) + EOT
        with self.assertRaises(ProjectionError) as ctx:
            self._project_events(payload)
        self.assertEqual(ctx.exception.code, "note_release_without_on")
        self.assertEqual(ctx.exception.tick, 0)

    def test_double_release(self):
        payload = (
            note_on(0, 0, 60, 100)
            + note_off(10, 0, 60)
            + note_off(10, 0, 60)
            + EOT
        )
        with self.assertRaises(ProjectionError) as ctx:
            self._project_events(payload)
        self.assertEqual(ctx.exception.code, "note_release_without_on")
        self.assertEqual(ctx.exception.tick, 20)

    def test_unclosed_note_at_end_of_file(self):
        payload = note_on(0, 0, 60, 100) + EOT
        with self.assertRaises(ProjectionError) as ctx:
            self._project_events(payload)
        self.assertEqual(ctx.exception.code, "note_unclosed")
        self.assertEqual(ctx.exception.tick, 0)

    def test_still_sustaining_at_end_of_file(self):
        payload = (
            pedal(0, 0, 127)
            + note_on(0, 0, 60, 100)
            + note_off(100, 0, 60)
            + EOT
        )
        with self.assertRaises(ProjectionError) as ctx:
            self._project_events(payload)
        self.assertEqual(ctx.exception.code, "note_unclosed")

    def test_cross_track_release_before_attack_at_same_tick_fails(self):
        # At tick 120 track 1 comes after track 0; swap roles so the release
        # is seen first: it must be rejected.
        t0 = note_off(120, 0, 60) + EOT
        t1 = note_on(120, 0, 60, 100) + EOT
        data = header(1, 2, 480) + track(t0) + track(t1)
        with self.assertRaises(ProjectionError) as ctx:
            normalize(data, projection=AUDIBLE_NOTES_PROJECTION)
        self.assertEqual(ctx.exception.code, "note_release_without_on")
        self.assertEqual(ctx.exception.track, 0)

    def test_events_available_on_projection_engine_directly(self):
        from app.midi import parse

        payload = note_on(0, 3, 55, 77) + note_off(5, 3, 55, 64) + EOT
        parsed = parse(header(1, 1, 480) + track(payload))
        ordered = sorted(
            parsed.channel_events, key=lambda e: (e.tick, e.track, e.order)
        )
        notes = project_audible_notes(ordered)
        self.assertEqual(notes[0].channel, 3)
        self.assertEqual(notes[0].velocity, 77)
        self.assertEqual((notes[0].start_tick, notes[0].end_tick), (0, 5))


if __name__ == "__main__":
    unittest.main()
