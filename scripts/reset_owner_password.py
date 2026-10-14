"""Reset the owner's password (PRD AUTH-8, DEPLOYMENT.md section 16).

Run by the developer on the server, never from the app:

    python -m scripts.reset_owner_password

It asks for the owner's phone number or email, then for the new password twice without
showing it. It works on the owner account only; a member's password is set by the
owner from the Account screen. The change is recorded as auth.password_changed with
"reset script". The password is never printed or logged.
"""

import getpass
import sys
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.database import get_session_factory
from app.core.errors import AppError, unexpected_error
from app.core.logging import configure_logging
from app.features.account import service as account


def main(
    ask: Callable[[str], str] = input,
    ask_hidden: Callable[[str], str] = getpass.getpass,
    say: Callable[[str], None] = print,
    open_session: Callable[[], Session] | None = None,
) -> int:
    """Returns 0 when the password was changed, 1 when nothing was changed."""
    configure_logging()
    identifier = ask("Owner's phone number or email: ")
    new_password = ask_hidden("New password (it will not be shown): ")
    typed_again = ask_hidden("Type the new password again: ")
    if new_password != typed_again:
        say("The two passwords are not the same. Nothing was changed.")
        return 1

    try:
        with (open_session or get_session_factory())() as db:
            owner = account.reset_owner_password(db, identifier, new_password)
            # read the name while the session is open: after it closes the record cannot be reloaded
            owner_name = f"{owner.first_name} {owner.last_name}"
    except AppError as exc:
        say(f"{exc.message} Nothing was changed.")
        return 1
    except Exception as exc:
        say(f"{unexpected_error(exc, 'reset the owner password').message} Nothing was changed.")
        return 1

    say(f"The password for {owner_name} has been changed. Give it to them in person.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
