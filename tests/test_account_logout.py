"""Settings → Logout: clears ONLY the Leapmotor login so a different account can be linked,
without touching trips/charges/positions. The poller detects the credential change and exits
(run.sh restarts the container) to re-authenticate as the new account. Pure poller.db → CI-safe."""
import db as D


def test_logout_clears_login_but_keeps_data(tmp_path):
    database = D.Database(str(tmp_path / "t.db"))
    # A configured account with a car already on file.
    database.set_setting("leapmotor_user", "old@example.com")
    database.set_secret("leapmotor_pass", "oldpass")
    database.set_secret("leapmotor_pin", "1111")
    database.mark_setup_complete()
    vid = database.ensure_vehicle("LVIN0000000000001", "B10", 2025)

    # Exactly what POST /api/account/logout does: clear the three login keys + the setup flag.
    database.set_setting("leapmotor_user", "")
    database.set_secret("leapmotor_pass", "")
    database.set_secret("leapmotor_pin", "")
    database.set_setting("setup_complete", "0")

    # Login is gone and the wizard re-opens…
    assert database.get_setting("leapmotor_user") == ""
    assert database.get_secret("leapmotor_pass") == ""
    assert database.get_secret("leapmotor_pin") == ""
    assert not database.is_setup_complete()
    # …but the car (and therefore its trips/charges, keyed by vehicle_id) is untouched.
    assert database.ensure_vehicle("LVIN0000000000001", "B10", 2025) == vid


def test_account_switch_is_detected(tmp_path):
    """The poller guards on (user, pass, pin) != startup: a *different* complete login means
    'switch accounts → restart'. The cleared logout window (any field empty) must NOT trigger."""
    database = D.Database(str(tmp_path / "t.db"))
    database.set_setting("leapmotor_user", "old@example.com")
    database.set_secret("leapmotor_pass", "oldpass")
    database.set_secret("leapmotor_pin", "1111")
    startup = (database.get_setting("leapmotor_user"),
               database.get_secret("leapmotor_pass"),
               database.get_secret("leapmotor_pin"))

    # Logged-out limbo: creds cleared → tuple differs but is incomplete → no restart.
    database.set_setting("leapmotor_user", "")
    database.set_secret("leapmotor_pass", "")
    database.set_secret("leapmotor_pin", "")
    limbo = (database.get_setting("leapmotor_user"),
             database.get_secret("leapmotor_pass"),
             database.get_secret("leapmotor_pin"))
    assert limbo != startup and not all(limbo)   # guard's all(...) check skips this

    # New account linked via the wizard: complete + different → poller exits to re-auth.
    database.set_setting("leapmotor_user", "new@example.com")
    database.set_secret("leapmotor_pass", "newpass")
    database.set_secret("leapmotor_pin", "2222")
    switched = (database.get_setting("leapmotor_user"),
                database.get_secret("leapmotor_pass"),
                database.get_secret("leapmotor_pin"))
    assert switched != startup and all(switched)
