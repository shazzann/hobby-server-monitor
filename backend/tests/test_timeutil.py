from hsm.timeutil import iso, parse


def test_parse_accepts_lxd_nanosecond_timestamps():
    assert iso(parse("2026-10-07T00:52:38.635871754Z")) == "2026-10-07T00:52:38Z"
    assert parse("2026-10-07T00:52:38.635871754Z").microsecond == 635871
    assert iso(parse("2026-10-07T00:52:38Z")) == "2026-10-07T00:52:38Z"
    assert iso(parse("2026-10-07T06:22:38.5+05:30")) == "2026-10-07T00:52:38Z"
