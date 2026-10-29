# C&C H2O Water Billing

A single-business water billing web app for C&C H2O Water Supplies. A meter
reading goes in, and the bill, SMS, running balance and reports follow.

Stack: FastAPI, Jinja2, HTMX, SQLAlchemy, Alembic and MySQL. One
server-rendered Python app, no separate frontend.

## Documents

All planning documents are in `docs/`. Start with `AGENTS.md` at the root, then:

- `docs/PRD.md`: what to build and the business rules
- `docs/TASKS.md`: the build plan, one task at a time
- `docs/architecture.md`: folder structure and how a request flows
- `docs/SECURITY.md`, `docs/AUDIT_LOG.md`, `docs/DESIGN_SYSTEM.md`
- `docs/DEPLOYMENT.md`: server steps (documented only)

## Run locally

Python 3.14 (the version the pinned packages were set up and tested with).
Create a virtual environment and install the packages:

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements-dev.txt
```

Copy `.env.example` to `.env` and fill in the values (never commit `.env`).
Create the development and test databases with `scripts/dev_db_setup.sql`, run
by the MySQL admin once.

```
# run the app (SMS_BACKEND=console in .env, so no real SMS is sent)
uvicorn app.main:app --reload

# tests
pytest

# apply migrations to the development database
alembic upgrade head
```

## Run with Docker on your computer

This is the same packaging the server uses (`docs/DEPLOYMENT.md`), so you can test it before
deploying. It needs Docker Desktop. Nothing here touches the server. Never put real passwords
in a file that is committed: every `.env*` file below except the `.example` ones is ignored by Git.

Two services run: `water-app` (the app) and `water-db` (MySQL 8.4). Neither publishes a port in the
committed `docker-compose.yml`.

The commands are for PowerShell, from the project folder. The folder name matters: Docker
Compose acts on the project in the folder you are in.

**1. Create the shared network once.** On the server this network is payroll's. Here it is an empty
stand-in, so `water-app` has the same two networks it has in production.

```
docker network create payroll_default
```

**2. Make the settings files.** Copy each example, then replace every `<...>` placeholder. Make
four different passwords with `python -c "import secrets; print(secrets.token_hex(24))"` (hex has no
characters that break a database address).

```
copy .env.example .env.docker
copy .env.db.example .env.db
copy .env.migrate.example .env.migrate
copy .env.backup.example .env.backup
```

- `.env.docker`: the app settings. Keep `APP_ENV=development` and `SMS_BACKEND=console` (or `phone`)
  so the page works over plain `http` and no real SMS is sent. Delete the commented local line.
- `.env.db`: the database root password. Only `water-db` gets it.
- `.env.migrate` and `.env.backup`: the migration and backup logins, with their passwords.

Your existing `.env` (for running without Docker) is left alone. The next step points Docker at
`.env.docker` instead.

**3. Make the local override file.** `docker-compose.override.yml` is ignored by Git. It points the app
at `.env.docker` and publishes the app on your own computer only (`127.0.0.1`), so you can open it
in a browser. Never create this file on the server.

```
services:
  water-app:
    env_file: !override
      - .env.docker
    ports:
      - "127.0.0.1:8001:8001"
```

Compose reads this file by itself. To see the committed file alone (with no ports), add
`-f docker-compose.yml` to a command, for example `docker compose -f docker-compose.yml config`.

**4. Build, then start the database.**

```
docker compose build
docker compose up -d water-db
docker compose ps
```

Wait until `water-db` shows `healthy`.

**5. Create the three database users** (`docs/DEPLOYMENT.md` section 6.4). Open an admin prompt:

```
docker compose exec water-db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" water_billing'
```

Paste the `CREATE USER` and `GRANT` lines from section 6.4, using the passwords from your settings
files, then type `exit`.

**6. Create the tables as the migration user**, then give the app user its rights (section 9.2):

```
$url = (Get-Content .env.migrate | Where-Object { $_ -like 'DATABASE_URL=*' }) -replace '^DATABASE_URL=', ''
docker compose run --rm --no-deps -e DATABASE_URL="$url" water-app alembic upgrade head
```

Open the admin prompt again and paste the `GRANT` lines from section 9.2.

**7. Start the app and open it.**

```
docker compose up -d
docker compose ps
```

Open http://127.0.0.1:8001. Create the owner account first: registration closes once it exists.

**Useful commands**

```
docker compose logs --tail 50 water-app      # what the app says
docker compose up -d --force-recreate water-app   # after changing .env.docker (restart does not re-read it)
docker compose down                          # stop; the data is kept
```

Never add `-v` to `docker compose down`: it deletes the database volume. Never run
`docker system prune` or `docker volume prune` on any machine that holds other projects.
