FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies before code, so editing the script does not invalidate the install layer.
COPY requirements.txt optional-requirements.txt ./
# PyGObject is libnotify for Linux desktops - useless in a container and it needs GTK headers.
RUN sed '/PyGObject/d' optional-requirements.txt > /tmp/opt.txt \
 && pip install --no-cache-dir -r requirements.txt -r /tmp/opt.txt \
 && rm /tmp/opt.txt

# Only the script. luxmed-ids/ is a lookup table for humans, the code never reads it.
COPY luxmed_sniper.py ./

RUN useradd --create-home --uid 10001 sniper \
 && mkdir /data && chown sniper:sniper /data
USER sniper

# CWD inside the volume, so a relative misc.notifydb lands somewhere persistent.
WORKDIR /data

# work() swallows every cycle failure so one bad cycle cannot kill the scheduler, which also
# means a container with wrong credentials stays "Up" forever. Each cycle that actually reached
# the portal writes its own expiry here; going stale is the only signal that nothing works.
ENV LUXMED_HEARTBEAT_FILE=/data/heartbeat
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=2 \
  CMD python3 -c "import os,pathlib,sys,time; p=pathlib.Path(os.environ['LUXMED_HEARTBEAT_FILE']); sys.exit(0 if p.is_file() and time.time() < float(p.read_text()) else 1)"

# The interval is deliberately not set here: docker-compose.yml owns it, and an exec-form CMD
# cannot expand variables anyway, so the script reads LUXMED_INTERVAL itself. A bare docker run
# therefore falls back to the script's own default.
ENTRYPOINT ["python3", "/app/luxmed_sniper.py"]
CMD ["-c", "/config/luxmed_sniper.yaml"]
