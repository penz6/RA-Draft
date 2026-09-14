from datetime import datetime, timezone
import unittest
from staff_event_schedule import next_occurrence, parse_repeat_weeks, parse_event_start, repeat_label, SCHOOL_TIMEZONE


class StaffEventScheduleTests(unittest.TestCase):
    def local(self, value):
        return datetime.fromisoformat(value).replace(tzinfo=SCHOOL_TIMEZONE)

    def test_blank_and_zero_are_one_time(self):
        for raw in (None, '', '0', 0):
            self.assertEqual(parse_repeat_weeks(raw), 0)
        self.assertEqual(next_occurrence('2026-09-01T18:00', 0, now=self.local('2026-10-01T12:00')), '2026-09-01T18:00')

    def test_interval_validation(self):
        for raw in ('-1', '1.5', '53', '1e2', 'weekly', True, '999999999999999'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_repeat_weeks(raw)
        self.assertEqual(parse_repeat_weeks(' 2 '), 2)
        self.assertEqual(parse_repeat_weeks(52), 52)

    def test_next_occurrence_preserves_anchor(self):
        start = '2026-09-01T18:00'
        for now, expected in (('2026-08-31T18:00',start),('2026-09-01T18:00:59',start),('2026-09-01T18:01','2026-09-15T18:00'),('2026-10-01T10:00','2026-10-13T18:00')):
            with self.subTest(now=now):
                self.assertEqual(next_occurrence(start, 2, now=self.local(now)), expected)

    def test_spring_and_fall_dst_keep_dinner_at_same_local_time(self):
        self.assertEqual(next_occurrence('2026-03-01T18:00', 1, now=self.local('2026-03-08T17:30')), '2026-03-08T18:00')
        self.assertEqual(next_occurrence('2026-10-25T18:00', 1, now=self.local('2026-11-01T17:30')), '2026-11-01T18:00')

    def test_future_gap_moves_forward_without_shifting_later_cycles(self):
        start = '2026-03-01T02:30'
        self.assertEqual(next_occurrence(start, 1, now=self.local('2026-03-08T03:00')), '2026-03-08T03:30')
        self.assertEqual(next_occurrence(start, 1, now=self.local('2026-03-08T03:31')), '2026-03-15T02:30')

    def test_year_boundary_and_many_missed_cycles(self):
        self.assertEqual(next_occurrence('2026-12-28T19:00', 2, now=self.local('2027-01-01T12:00')), '2027-01-11T19:00')
        result = next_occurrence('2000-01-03T18:00', 3, now=self.local('2026-09-14T12:00'))
        actual = datetime.fromisoformat(result)
        self.assertEqual((actual-datetime(2000,1,3,18)).days % 21, 0)
        self.assertGreaterEqual(actual, datetime(2026,9,14,12))

    def test_utc_now_uses_school_wall_clock(self):
        self.assertEqual(next_occurrence('2026-09-14T18:00', 1, now=datetime(2026,9,14,22,0,tzinfo=timezone.utc)), '2026-09-14T18:00')

    def test_invalid_or_missing_stored_values_are_safe(self):
        self.assertIsNone(next_occurrence(None,2))
        self.assertIsNone(next_occurrence('broken',2))
        self.assertIsNone(next_occurrence('2026-09-01T18:00',100))
        self.assertIsNone(next_occurrence('9999-12-30T18:00',52,now=self.local('9999-12-31T19:00')))

    def test_event_form_rejects_offsets_dates_without_time_and_spring_gap(self):
        for raw in ('2026-09-01','2026-09-01T18:00Z','2026-09-01T18:00+01:00','2026-02-30T18:00','2026-03-08T02:30'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_event_start(raw)
        self.assertEqual(parse_event_start('2026-11-01T01:30'), '2026-11-01T01:30')

    def test_repeat_labels(self):
        self.assertEqual(repeat_label(0),'')
        self.assertEqual(repeat_label(1),'Every week')
        self.assertEqual(repeat_label(3),'Every 3 weeks')


if __name__ == '__main__':
    unittest.main()
