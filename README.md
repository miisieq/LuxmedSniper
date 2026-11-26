# LuxmedSniper
LUX MED appointments sniper
=======================================
Simple tool to notify about available slot in LUX MED medical care service using pushover notifications.

How to use LuxmedSniper?
--------------------
First of all create a virtualenv and install the requirements - the exact commands are in
[Running locally without Docker](#running-locally-without-docker) below.

1) Copy the template and fill in your credentials. The copy is gitignored, the template is not
   - never put real credentials in `luxmed_sniper.example.yaml`:
```
cp luxmed_sniper.example.yaml luxmed_sniper.yaml
```
Every option is documented inline in the template: which ids go into `doctor_locators`,
which notification providers exist and what each one needs. City and service ids can be
looked up in `luxmed-ids/`, or regenerated with `python3 luxmed_sniper.py --dump-ids`.

City and service ids are cheap to refresh (two requests) and live in the repo. Facility and
doctor ids are not: they need `--dump-ids-doctors`, which issues one request per city and
service, so narrow it with `--dump-ids-city` and mind the fair use warning at the bottom of
this file:
```
python3 luxmed_sniper.py --dump-ids --dump-ids-doctors --dump-ids-city "Trojmiasto"
```
The resulting `ids-facilities-doctors-*.json` is **gitignored on purpose** - it pairs doctors'
real names with the clinic and service they work at, which is personal data taken from behind
the portal's login, and nothing in the code reads it. Keep it local; it is there for you to
search when composing `doctor_locators`.

Configuration for several people goes into separate files - name them `luxmed_<user>.yml`
or `local-<user>.yaml`, both of which are gitignored.

2) Run it
```
python3 luxmed_sniper.py
```
`luxmed_sniper.yaml` is the default, so `-c` is only needed for a different path. Several
files can be merged, which is handy for splitting users, doctors and providers apart:
```
python3 luxmed_sniper.py -c user_config.yml luxmed_john.yml
```
Note that merging cannot override a value - a key present in two files must hold the same
value in both, otherwise the script stops with a conflict error.

3) Wait for new appointment notifications in your pushover app on mobile :)!

Running locally without Docker
--------------------
The Docker image runs Python 3.12, so use the same locally. Create the virtualenv once:
```
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r dev-requirements.txt
```
`requirements.txt` is enough to run the script; `dev-requirements.txt` adds pytest and ruff.
`optional-requirements.txt` is only needed for the slack, pushbullet, telegram and gi providers -
pushover, ntfy and console work without it.

With `luxmed_sniper.yaml` prepared as in step 1, start it from the repository root:
```
python3 luxmed_sniper.py
```
The first cycle runs immediately and the script then repeats every 1800 seconds until stopped
with Ctrl+C. The config is re-read every cycle, so an edit takes effect without a restart - except
the Pushover credentials used for outage alerts, which are read once at startup.

**Search interval.** `-d` takes seconds, and `LUXMED_INTERVAL` does the same from the environment:
```
python3 luxmed_sniper.py -d 3600
```
Do not shorten it to speed up testing - every start logs in and searches for real, see the
warning at the bottom of this file.

**Debug logs.** They include the query parameters of every search; the password and tokens are
masked:
```
LOGURU_LEVEL=DEBUG python3 luxmed_sniper.py
```

**Trying things out safely.** The notifications are real too. To watch what would be sent
without sending it, point a copy of the config at the console provider only
(`notification_provider: [console]`) and pass it with `-c`.

**Notification state** is the file named by `misc.notifydb`, relative to the directory the
script is started from. It is not shared with the Docker volume, so a local run next to a
running container announces the same slots a second time.

**Time zone.** The search window starts at today's date on the system clock, so the machine
should be set to Polish time (`Europe/Warsaw`); the compose file sets it for the container.

**Tests and linting** never touch the network:
```
pytest -q
ruff check .
```

Running with Docker
--------------------
Prepare `luxmed_sniper.yaml` as in step 1 - it stays on the host and is bind-mounted read-only,
so credentials never reach an image layer. State lives in a named volume rather than in the
container. Then:
```
docker compose up -d --build
```
Logs are the only place history lives, and the compose file sets no size limit, so cap them at
the daemon level in `/etc/docker/daemon.json` if the host needs it:
```
docker compose logs -f
```

**Search interval.** Set by `LUXMED_INTERVAL` in `docker-compose.yml`, 900 seconds by default.
The script reads the variable itself, so changing it needs no rebuild and no `command:` override:
edit the value and run
```
docker compose up -d
```
Outside a container, where the variable is unset, the default is 1800.

**After editing the config, restart.** The script re-reads its config every cycle, but most
editors replace the file instead of writing into it, and a single-file bind mount follows the
original inode - the container then sees no config at all. A restart re-resolves the mount:
```
docker compose restart
```

**Notification state** lives in a named volume, so it survives `docker compose down` and
rebuilds. A fresh or renamed volume starts with an empty history, and the first cycle then
notifies about every slot that is currently free.

# Warning

Please be advised that running too many queries against LuxMed API may result in locking your LuxMed account.
Breaching the 'fair use policy' for the first time locks the account temporarily for 1 day.
Breaching it again locks it indefinitely and manual intervention with "Patient Portal Support" is required to unlock it.
