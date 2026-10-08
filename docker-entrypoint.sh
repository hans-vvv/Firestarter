#!/bin/sh
# Container entrypoint for the Firestarter demo.
# It runs first (as PID 1), prepares the data root, then hands PID 1 over to
# the real program with "exec" (gunicorn, or whatever command was given).
#
# Only when the data root is EMPTY (no app.db yet) it is filled, in this order:
#   1. /bundle   a ready-made data bundle (an unzipped firestarter-data-demo-*.zip)
#                mounted by the user: same layout as the data root, one copy.
#   2. the seed  demo INPUTS (topology.xlsx, services/, compliance/, simulation/).
#                Default: the starter kit inside the image. Mount your own inputs
#                and point FIRESTARTER_SEED at them to build a different network.
#                They are copied in and scripts/bootstrap_demo.py builds the rest.
# Once app.db exists, the volume is live data and is never touched again.
set -e

APP_DIR="${FIRESTARTER_APP_DIR:-/opt/firestarter}"
DATA="${FIRESTARTER_DATA:-/data}"
BUNDLE="${FIRESTARTER_BUNDLE:-/bundle}"
SEED="${FIRESTARTER_SEED:-$APP_DIR/demo-seed}"

if [ ! -f "$DATA/app.db" ]; then
    if [ -f "$BUNDLE/app.db" ]; then
        echo "entrypoint: empty data root, copying the data bundle from $BUNDLE"
        cp -R "$BUNDLE/." "$DATA/"
    elif [ -f "$SEED/topology.xlsx" ]; then
        echo "entrypoint: empty data root, building the demo from $SEED"
        for p in topology.xlsx services compliance simulation; do
            if [ -e "$SEED/$p" ]; then
                cp -R "$SEED/$p" "$DATA/"
            fi
        done
        # Inputs only: drop generated output that may sit in a mounted checkout.
        rm -rf "$DATA/compliance/reports"
        python "$APP_DIR/scripts/bootstrap_demo.py"
        echo "entrypoint: demo ready"
    else
        echo "entrypoint: empty data root and no bundle or seed found;"
        echo "entrypoint: the dashboard starts with an empty database"
    fi
fi

exec "$@"
