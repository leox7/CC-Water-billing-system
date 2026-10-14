"""The login attempt limit on its own, with a clock the test controls (AUTH-S5)."""

from app.core.security import LoginAttemptLimiter, login_key

MINUTE = 60


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += minutes * MINUTE


def make_limiter():
    clock = FakeClock()
    return LoginAttemptLimiter(clock=clock), clock


def test_nobody_is_locked_at_the_start():
    limiter, _ = make_limiter()

    assert limiter.seconds_locked("254722123456") == 0


def test_four_failures_do_not_lock_and_the_fifth_does():
    limiter, _ = make_limiter()

    for expected_count in (1, 2, 3, 4):
        assert limiter.record_failure("254722123456") == expected_count
        assert limiter.seconds_locked("254722123456") == 0

    assert limiter.record_failure("254722123456") == 5
    assert limiter.seconds_locked("254722123456") == 15 * MINUTE


def test_the_lock_lasts_fifteen_minutes_and_then_counting_starts_again():
    limiter, clock = make_limiter()
    for _ in range(5):
        limiter.record_failure("254722123456")

    clock.advance(14.9)
    assert limiter.seconds_locked("254722123456") > 0

    clock.advance(0.1)
    assert limiter.seconds_locked("254722123456") == 0
    assert limiter.record_failure("254722123456") == 1


def test_failures_older_than_fifteen_minutes_do_not_count():
    limiter, clock = make_limiter()
    for _ in range(4):
        limiter.record_failure("254722123456")

    clock.advance(15)

    assert limiter.record_failure("254722123456") == 1
    assert limiter.seconds_locked("254722123456") == 0


def test_failures_spread_inside_the_window_still_lock():
    limiter, clock = make_limiter()
    for _ in range(5):
        limiter.record_failure("254722123456")
        clock.advance(3)      # five failures over twelve minutes

    assert limiter.seconds_locked("254722123456") > 0


def test_each_phone_or_email_is_counted_on_its_own():
    limiter, _ = make_limiter()
    for _ in range(5):
        limiter.record_failure("254722123456")

    assert limiter.seconds_locked("254733000000") == 0
    assert limiter.seconds_locked("owner@example.com") == 0


def test_clear_forgets_the_failures_and_lifts_the_lock():
    limiter, _ = make_limiter()
    for _ in range(5):
        limiter.record_failure("254722123456")

    limiter.clear("254722123456")

    assert limiter.seconds_locked("254722123456") == 0
    assert limiter.record_failure("254722123456") == 1


def test_old_entries_are_cleaned_out_so_memory_does_not_grow_forever():
    limiter, clock = make_limiter()
    limiter.CLEAN_UP_AFTER = 3
    for key in ("a", "b", "c"):
        limiter.record_failure(key)
    clock.advance(16)

    limiter.record_failure("d")

    assert set(limiter._failure_times) == {"d"}


def test_a_lock_is_never_cleaned_out_early():
    limiter, clock = make_limiter()
    limiter.CLEAN_UP_AFTER = 2
    for _ in range(5):
        limiter.record_failure("locked")
    clock.advance(10)

    limiter.record_failure("other")
    limiter.record_failure("another")

    assert limiter.seconds_locked("locked") > 0


def test_the_key_is_the_stored_form_of_the_phone_or_email():
    assert login_key("0722 123 456") == login_key("+254722123456") == "254722123456"
    assert login_key(" Owner@Example.com ") == "owner@example.com"
    assert login_key(" Something Else ") == "something else"
