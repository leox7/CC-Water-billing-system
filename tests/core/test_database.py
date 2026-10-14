from sqlalchemy import text


def test_connects_as_the_limited_user_not_root(db_session):
    # DB-2: the app and tests never use root
    current_user = db_session.execute(text("SELECT CURRENT_USER()")).scalar()

    assert not current_user.lower().startswith("root@")


def test_connection_time_zone_is_east_africa_time(db_session):
    # DEPLOYMENT.md section 8: every connection runs SET time_zone = '+03:00'
    time_zone = db_session.execute(text("SELECT @@session.time_zone")).scalar()

    assert time_zone == "+03:00"
