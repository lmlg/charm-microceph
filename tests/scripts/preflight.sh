#!/usr/bin/env bash
#
# CI preflight probes: detect a gross Snap Store, Ubuntu archive or CharmHub
# outage before the self-hosted test matrix starts, so the run records one
# clearly named infra failure instead of one failure per suite. Every heavy
# job here bootstraps Juju (Snap Store agent binaries), most install a charm
# from CharmHub (ch: charms/bundles, or `juju deploy microceph`/`ceph-mon`
# etc. by name), and a few also pull Ubuntu packages.
#
# Usage: preflight.sh <probe> [args...]
#
#   probe_endpoints [name=url ...]  Probe the Snap Store, Ubuntu archive and
#                                   CharmHub, plus any extra endpoints given.
#   snap_canary                     Download and verify a small snap.
#   charmhub_canary [charm ...]     Resolve charm info (default: microceph).
#
# The probes install nothing and only write inside temporary directories.
#
# Modeled on the same-named script in canonical/microceph
# (https://github.com/canonical/microceph/pull/852,
# https://github.com/canonical/microceph/blob/main/tests/scripts/preflight.sh).

set -eu

# Report an infra failure as a GitHub error annotation and step summary.
# The "kind=preflight" prefix is what failure classification keys on.
preflight_fail() {
    local message="kind=preflight PREFLIGHT: $1"
    echo "::error title=Infra::${message}"
    echo "${message}" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    exit 1
}

PREFLIGHT_FAILURES=""

# Temporary directory for the canaries. It is script-level, not function-local,
# because the EXIT trap runs after the probe function has returned.
PREFLIGHT_WORK_DIR=""

cleanup() {
    if [ -n "$PREFLIGHT_WORK_DIR" ]; then
        rm -rf "$PREFLIGHT_WORK_DIR"
    fi
}
trap cleanup EXIT

# Probe one URL; extra arguments are passed to curl. Failures accumulate in
# PREFLIGHT_FAILURES so one run reports every unreachable endpoint.
check_endpoint() {
    local name="$1" url="$2"
    shift 2
    # Keep curl's exponential backoff (1s, 2s) and honor Retry-After.
    if curl -sSf --max-time 10 --retry 2 --retry-connrefused \
        --retry-max-time 30 "$@" "$url" >/dev/null 2>&1; then
        echo "$name reachable: $url"
    else
        PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES:+$PREFLIGHT_FAILURES, }$name ($url)"
    fi
}

probe_endpoints() {
    local extra
    # api.snapcraft.io answers 400 to info queries without the
    # device-series header, so the probe would never pass without it.
    check_endpoint snap-store "https://api.snapcraft.io/v2/snaps/info/lxd" -H "Snap-Device-Series: 16"
    check_endpoint ubuntu-archive "http://archive.ubuntu.com/ubuntu/dists/"
    # api.charmhub.io/v2/charms/refresh (the endpoint `juju deploy` actually
    # calls) is POST-only and needs a signed request body; /info/<charm> is
    # a cheap GET against the same backend and fails the same way on an
    # outage (see the EOF on refresh in
    # https://github.com/canonical/charm-microceph/issues/359).
    check_endpoint charmhub "https://api.charmhub.io/v2/charms/info/microceph"
    for extra in "$@"; do
        check_endpoint "${extra%%=*}" "${extra#*=}"
    done

    if [ -n "$PREFLIGHT_FAILURES" ]; then
        preflight_fail "endpoint checks failed: $PREFLIGHT_FAILURES"
    fi
}

snap_canary() {
    local status
    PREFLIGHT_WORK_DIR=$(mktemp -d)
    cd "$PREFLIGHT_WORK_DIR"

    # Download and verify a small snap and its assertions; install nothing.
    if timeout --kill-after=5s 60s snap download hello-world --channel=latest/stable; then
        echo "Snap Store payload and assertions verified"
    else
        status=$?
        preflight_fail "Snap Store payload/assertion download failed (exit $status)"
    fi
}

charmhub_canary() {
    local charm status
    if [ "$#" -eq 0 ]; then
        set -- microceph
    fi

    for charm in "$@"; do
        if status=$(curl -sSf --max-time 10 --retry 2 --retry-connrefused \
            --retry-max-time 30 "https://api.charmhub.io/v2/charms/info/${charm}" 2>&1); then
            echo "CharmHub info resolved for ${charm}: $(echo "$status" | head -c 200)"
        else
            preflight_fail "CharmHub info lookup failed for ${charm}: $status"
        fi
    done
}

run="${1:?usage: preflight.sh <probe_endpoints|snap_canary|charmhub_canary> [args...]}"
shift

$run "$@"
