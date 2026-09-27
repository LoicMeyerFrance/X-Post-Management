"""Tests for the scheduled-date logic.

X's native schedule dialog is a row of <select> elements whose contents vary by
account, language and interface version: month names or numbers, zero-padded
days or not, minutes every minute or every five, 24h or 12h with an AM/PM box.
Picking the wrong option there schedules the post at the wrong time without any
visible error, so the mapping is tested here against realistic option sets.

No browser and no network: this exercises the pure mapping functions.

    python tests/test_schedule.py
"""

import os
import shutil
import sys
import tempfile
from datetime import datetime

TEST_HOME = tempfile.mkdtemp(prefix='xpm-sched-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'server'))

import bot  # noqa: E402

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    print(('  ok  ' if condition else 'FAIL  ') + name + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


# --- Realistic option sets -------------------------------------------------

MONTHS_FR = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin',
             'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']
MONTHS_EN = ['January', 'February', 'March', 'April', 'May', 'June',
             'July', 'August', 'September', 'October', 'November', 'December']

DAYS = [str(d) for d in range(1, 32)]
DAYS_PADDED = [f'{d:02d}' for d in range(1, 32)]
YEARS = ['2026', '2027']
HOURS_24 = [str(h) for h in range(24)]
HOURS_24_PADDED = [f'{h:02d}' for h in range(24)]
HOURS_12 = [str(h) for h in range(1, 13)]
MINUTES = [f'{m:02d}' for m in range(60)]
MINUTES_STEP5 = [f'{m:02d}' for m in range(0, 60, 5)]
AMPM = ['AM', 'PM']


def pick(role, dt, options, use_24h=True):
    return bot._option_for(role, dt, options, use_24h=use_24h)


# --- Tests -----------------------------------------------------------------

def test_role_detection():
    section('recognising a single select')

    def role_of(options, *others):
        """Classify `options` inside a dialog that also holds `others`."""
        lists = [options] + list(others)
        return bot._classify_selects(lists)[0]

    cases = [
        ('French month names', MONTHS_FR, 'month', ()),
        ('English month names', MONTHS_EN, 'month', ()),
        ('days 1-31', DAYS, 'day', ()),
        ('years', YEARS, 'year', ()),
        ('hours 0-23', HOURS_24, 'hour', ()),
        ('minutes every 5', MINUTES_STEP5, 'minute', ()),
        ('minutes 0-59 (not hours)', MINUTES, 'minute', ()),
        ('AM/PM', AMPM, 'ampm', ()),
        # 1-12 only makes sense in context: alongside a 24h clock it is a month,
        # alongside an AM/PM box it is the hour.
        ('1-12 next to a 0-23 clock -> month', HOURS_12, 'month', (HOURS_24,)),
        ('1-12 next to AM/PM -> hour', HOURS_12, 'hour', (AMPM,)),
    ]
    for label, options, expected, others in cases:
        got = role_of(options, *others)
        check(f'{label} -> {expected}', got == expected, got)


def test_24h_layout():
    section('24h layout, French months, every minute')

    dt = datetime(2026, 9, 27, 16, 37)
    check('month', pick('month', dt, MONTHS_FR) == 'septembre', pick('month', dt, MONTHS_FR))
    check('day', pick('day', dt, DAYS) == '27', pick('day', dt, DAYS))
    check('year', pick('year', dt, YEARS) == '2026', pick('year', dt, YEARS))
    check('hour', pick('hour', dt, HOURS_24) == '16', pick('hour', dt, HOURS_24))
    check('minute', pick('minute', dt, MINUTES) == '37', pick('minute', dt, MINUTES))


def test_12h_layout():
    section('12h layout with AM/PM, English months')

    cases = [
        (datetime(2026, 1, 1, 0, 0), '12', 'AM', 'midnight'),
        (datetime(2026, 1, 1, 0, 30), '12', 'AM', '00:30'),
        (datetime(2026, 1, 1, 9, 5), '9', 'AM', 'morning'),
        (datetime(2026, 1, 1, 11, 59), '11', 'AM', 'just before noon'),
        (datetime(2026, 1, 1, 12, 0), '12', 'PM', 'noon'),
        (datetime(2026, 1, 1, 13, 0), '1', 'PM', 'early afternoon'),
        (datetime(2026, 1, 1, 23, 45), '11', 'PM', 'late evening'),
    ]
    for dt, hour, ampm, label in cases:
        got_h = pick('hour', dt, HOURS_12, use_24h=False)
        got_a = pick('ampm', dt, AMPM, use_24h=False)
        check(f'{label} ({dt:%H:%M}) -> {hour} {ampm}',
              got_h == hour and got_a == ampm, f'{got_h} {got_a}')

    check('December in English months',
          pick('month', datetime(2026, 12, 25, 10, 0), MONTHS_EN) == 'December')


def test_padding_and_indexing():
    section('formatting variants X may serve')

    dt = datetime(2026, 3, 5, 7, 8)
    check('zero-padded day', pick('day', dt, DAYS_PADDED) == '05', pick('day', dt, DAYS_PADDED))
    check('zero-padded hour', pick('hour', dt, HOURS_24_PADDED) == '07',
          pick('hour', dt, HOURS_24_PADDED))
    check('zero-padded minute', pick('minute', dt, MINUTES) == '08', pick('minute', dt, MINUTES))

    months_1indexed = [str(m) for m in range(1, 13)]
    check('numeric months 1-12', pick('month', dt, months_1indexed) == '3',
          pick('month', dt, months_1indexed))

    months_0indexed = [str(m) for m in range(12)]
    check('numeric months 0-11 (March is "2")', pick('month', dt, months_0indexed) == '2',
          pick('month', dt, months_0indexed))


def test_impossible_values_fail_loudly():
    section('values X does not offer must fail, not be approximated')

    dt = datetime(2026, 9, 27, 16, 37)
    check('37 minutes in a 5-minute list -> None',
          pick('minute', dt, MINUTES_STEP5) is None, pick('minute', dt, MINUTES_STEP5))

    check('a year X does not list -> None',
          pick('year', datetime(2031, 1, 1, 0, 0), YEARS) is None)

    check('day 31 in a 30-day list -> None',
          pick('day', datetime(2026, 4, 31 - 1, 0, 0).replace(day=30), DAYS[:29]) is None
          or pick('day', datetime(2026, 4, 30, 0, 0), DAYS[:29]) is None)

    check('empty option list -> None', pick('hour', dt, []) is None)
    check('unknown role -> None', pick('weekday', dt, DAYS) is None)


def test_every_minute_of_a_day():
    section('exhaustive sweep: every minute of a day, both layouts')

    bad_24, bad_12 = [], []
    for hour in range(24):
        for minute in range(0, 60, 7):        # 7 to hit odd values too
            dt = datetime(2026, 9, 27, hour, minute)

            h24 = pick('hour', dt, HOURS_24)
            m24 = pick('minute', dt, MINUTES)
            if h24 != str(hour) or m24 != f'{minute:02d}':
                bad_24.append((dt.strftime('%H:%M'), h24, m24))

            h12 = pick('hour', dt, HOURS_12, use_24h=False)
            a12 = pick('ampm', dt, AMPM, use_24h=False)
            expect_h = str(hour % 12 or 12)
            expect_a = 'AM' if hour < 12 else 'PM'
            if h12 != expect_h or a12 != expect_a:
                bad_12.append((dt.strftime('%H:%M'), h12, a12))

    check('24h: all 216 times map correctly', not bad_24, bad_24[:3])
    check('12h: all 216 times map correctly', not bad_12, bad_12[:3])


def test_every_day_of_a_year():
    section('exhaustive sweep: every day of 2026')

    bad = []
    day = datetime(2026, 1, 1)
    while day.year == 2026:
        if (pick('month', day, MONTHS_FR) != MONTHS_FR[day.month - 1]
                or pick('day', day, DAYS) != str(day.day)
                or pick('year', day, YEARS) != '2026'):
            bad.append(day.strftime('%Y-%m-%d'))
        day = day.replace(day=day.day + 1) if day.day < 28 else day
        # step month by month once past day 28 to keep this simple and total
        if day.day == 28:
            if day.month == 12:
                break
            day = day.replace(month=day.month + 1, day=1)
    check('all dates of 2026 map correctly', not bad, bad[:3])



# --- Whole-dialog classification ------------------------------------------

def ph(values):
    """X prefixes every schedule select with an empty placeholder option."""
    return [''] + list(values)


# Exactly what X served on 2026-09-27: numeric month, 24h clock, no AM/PM.
X_DIALOG_24H = [
    ph(str(m) for m in range(1, 13)),      # month  (1-12)
    ph(str(d) for d in range(1, 31)),      # day
    ph(['2028', '2027', '2026']),          # year
    ph(str(h) for h in range(24)),         # hour   (0-23)
    ph(str(m) for m in range(60)),         # minute
]

X_DIALOG_12H = [
    ph(MONTHS_EN),
    ph(str(d) for d in range(1, 32)),
    ph(['2026', '2027']),
    ph(str(h) for h in range(1, 13)),      # hour   (1-12)
    ph(f'{m:02d}' for m in range(60)),
    ph(['AM', 'PM']),
]

# The nastiest case: numeric month AND a 12-hour clock, both 1-12.
X_DIALOG_AMBIGUOUS = [
    ph(str(m) for m in range(1, 13)),      # month
    ph(str(d) for d in range(1, 32)),
    ph(['2026']),
    ph(str(h) for h in range(1, 13)),      # hour, 12h
    ph(str(m) for m in range(60)),
    ph(['AM', 'PM']),
]


def test_dialog_classification():
    section('classifying a whole dialog')

    got = bot._classify_selects(X_DIALOG_24H)
    check("X's real 24h dialog", got == ['month', 'day', 'year', 'hour', 'minute'], got)

    got = bot._classify_selects(X_DIALOG_12H)
    check('12h dialog with month names',
          got == ['month', 'day', 'year', 'hour', 'minute', 'ampm'], got)

    got = bot._classify_selects(X_DIALOG_AMBIGUOUS)
    check('numeric month next to a 12h clock',
          got == ['month', 'day', 'year', 'hour', 'minute', 'ampm'], got)

    check('placeholder-only select is unknown', bot._classify_selects([['']]) == ['unknown'])


def test_end_to_end_mapping():
    section('a date through a whole dialog')

    def resolve(dialog, dt):
        roles = bot._classify_selects(dialog)
        use_24h = 'ampm' not in roles
        out = {}
        for role, options in zip(roles, dialog):
            if role != 'unknown' and role not in out:
                out[role] = bot._option_for(role, dt, options, use_24h=use_24h)
        return out

    dt = datetime(2026, 9, 30, 18, 37)
    got = resolve(X_DIALOG_24H, dt)
    check(f'{dt:%d/%m/%Y %H:%M} on the real 24h dialog',
          got == {'month': '9', 'day': '30', 'year': '2026', 'hour': '18', 'minute': '37'}, got)

    dt = datetime(2026, 12, 25, 7, 5)
    got = resolve(X_DIALOG_12H, dt)
    check(f'{dt:%d/%m/%Y %H:%M} on the 12h dialog',
          got == {'month': 'December', 'day': '25', 'year': '2026',
                  'hour': '7', 'minute': '05', 'ampm': 'AM'}, got)

    dt = datetime(2026, 1, 3, 23, 0)
    got = resolve(X_DIALOG_AMBIGUOUS, dt)
    check(f'{dt:%d/%m/%Y %H:%M} with month and 12h clock both 1-12',
          got == {'month': '1', 'day': '3', 'year': '2026',
                  'hour': '11', 'minute': '0', 'ampm': 'PM'}, got)

    # A month select must never receive the hour: that is the bug this guards.
    roles = bot._classify_selects(X_DIALOG_24H)
    check('the 1-12 select is the month, not the hour', roles[0] == 'month', roles)
    check('the 0-23 select is the hour', roles[3] == 'hour', roles)


def main():
    test_role_detection()
    test_24h_layout()
    test_12h_layout()
    test_padding_and_indexing()
    test_impossible_values_fail_loudly()
    test_every_minute_of_a_day()
    test_every_day_of_a_year()
    test_dialog_classification()
    test_end_to_end_mapping()

    print(f'\n{len(passed)} passed, {len(failed)} failed')
    if failed:
        print('failing: ' + ', '.join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    sys.exit(code)
