"""Entry point.

The application lives in the `luxmedsniper` package - this file is only what the Dockerfile's
ENTRYPOINT and the README's commands point at. Keeping the path stable is the whole job:
`python3 luxmed_sniper.py` works exactly as it did before the code was split up.
"""

from luxmedsniper.cli import main

if __name__ == "__main__":
    # SystemExit, not a bare call: main() returns 1 for a config that is not a readable file,
    # and a container reporting success on a missing bind mount would never be noticed.
    raise SystemExit(main())
