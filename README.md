# LuxmedSniper
LUX MED appointments sniper
=======================================
Simple tool to notify about available slot in LUX MED medical care service using pushover notifications.

How to use LuxmedSniper?
--------------------
First of all create virtualenv and install Python requirements from requirements.txt

1) Copy the template and fill in your credentials. The copy is gitignored, the template is not
   - never put real credentials in `luxmed_sniper.example.yaml`:
```
cp luxmed_sniper.example.yaml luxmed_sniper.yaml
```
Every option is documented inline in the template: which ids go into `doctor_locators`,
which notification providers exist and what each one needs. City and service ids can be
looked up in `luxmed-ids/`, or regenerated with `python3 luxmed_sniper.py --dump-ids`.

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
