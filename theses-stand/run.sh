#!/usr/bin/env bash
# Theses stand: recovery of a fleet after a mass loss of connections.
#
# For each repetition a fresh gateway-probe and fleet-emulator are started; DEVICES devices with SENSORS Probe Sensors
# each connect over CoAP over TLS (mutual X.509), register and are observed; then ALL connections are closed at once
# (no Deregister) and the devices reconnect with the client strategy RECONNECT. The probe's per-session event log and
# metric snapshots are stored under raw/<run-id>/rep<i>/. Figure and numbers: .venv/bin/python plot.py raw/<run-id>
#
# Parameters (environment variables):
#   DEVICES=1000  SENSORS=2  REPS=3  CONNECT_RATE=200 (warm-up start, devices/s)
#   RECONNECT=DECORRELATED_JITTER  BASE_DELAY=200ms  MAX_DELAY=5s
#   RUN_ID=<timestamp>  SKIP_BUILD=0  E1_JAVA_HOME (Java 25+)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="$(cd "$HERE/../.." && pwd)"
PROBE_DIR="$CODE/gateway-probe"
EMU_DIR="$CODE/fleet-emulator"
LIB_DIR="$CODE/lwm2m-objects"

DEVICES=${DEVICES:-1000}
SENSORS=${SENSORS:-2}
REPS=${REPS:-3}
CONNECT_RATE=${CONNECT_RATE:-200}
RECONNECT=${RECONNECT:-DECORRELATED_JITTER}
BASE_DELAY=${BASE_DELAY:-200ms}
MAX_DELAY=${MAX_DELAY:-5s}
RUN_ID=${RUN_ID:-$(date +%Y%m%d-%H%M%S)}
SKIP_BUILD=${SKIP_BUILD:-0}

PROBE_HTTP=18080
EMU_HTTP=18090
TLS_PORT=15684
TCP_PORT=15683
WAIT_TIMEOUT=180

if [[ -n "${E1_JAVA_HOME:-}" ]]; then
	JAVA_HOME=$E1_JAVA_HOME
elif [[ -x /usr/libexec/java_home ]] && /usr/libexec/java_home -v 25 >/dev/null 2>&1; then
	JAVA_HOME=$(/usr/libexec/java_home -v 25)
fi
export JAVA_HOME
JAVA="$JAVA_HOME/bin/java"
"$JAVA" -version 2>&1 | grep -qE 'version "(2[5-9]|[3-9][0-9])' || { echo "Java 25+ required (set E1_JAVA_HOME)"; exit 1; }

OUT="$HERE/raw/$RUN_ID"
mkdir -p "$OUT"
PIDS=()
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
cleanup() {
	for pid in "${PIDS[@]:-}"; do [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true; done
	PIDS=()
}
trap cleanup EXIT
metric() { curl -s "$1" | awk -v re="$2" '$0 ~ re && $0 !~ /^#/ { s += $NF } END { printf "%.0f\n", s + 0 }'; }
wait_metric() {
	local url=$1 re=$2 target=$3 deadline=$((SECONDS + WAIT_TIMEOUT))
	while (( SECONDS < deadline )); do
		(( $(metric "$url" "$re") >= target )) && return 0
		sleep 0.2
	done
	log "timeout waiting for $re >= $target (last: $(metric "$url" "$re"))"
	return 1
}
wait_http() {
	local deadline=$((SECONDS + WAIT_TIMEOUT))
	until curl -sf "$1" >/dev/null; do (( SECONDS < deadline )) || return 1; sleep 0.3; done
}
now() { python3 -c 'import time;print(time.time())'; }

if [[ "$SKIP_BUILD" != 1 ]]; then
	log "building"
	(cd "$LIB_DIR" && ./gradlew -q publishToMavenLocal)
	(cd "$PROBE_DIR" && ./gradlew -q bootJar)
	(cd "$EMU_DIR" && ./gradlew -q bootJar)
fi
PROBE_JAR=$(ls "$PROBE_DIR"/build/libs/gateway-probe-*-SNAPSHOT.jar | grep -v plain | head -1)
EMU_JAR=$(ls "$EMU_DIR"/build/libs/fleet-emulator-*-SNAPSHOT.jar | grep -v plain | head -1)
CERTS="$PROBE_DIR/certs"
[[ -f "$CERTS/ca.key" && -f "$CERTS/server.p12" ]] || (cd "$PROBE_DIR" && scripts/generate-dev-certs.sh >/dev/null)

git_rev() { git -C "$1" rev-parse --short HEAD 2>/dev/null || echo unknown; }
git_dirty() { [[ -n "$(git -C "$1" status --porcelain 2>/dev/null)" ]] && echo true || echo false; }
cat > "$OUT/environment.json" <<EOF
{
  "run_id": "$RUN_ID",
  "started": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "parameters": {
    "devices": $DEVICES, "sensors": $SENSORS, "repetitions": $REPS, "connect_rate": $CONNECT_RATE,
    "reconnect_strategy": "$RECONNECT", "base_delay": "$BASE_DELAY", "max_delay": "$MAX_DELAY",
    "transport": "coaps+tcp (TLS 1.2, mutual X.509, session resumption on)", "sample_period": "1s",
    "placement": "IN_MEMORY", "dispatch": "SINGLE", "io_threads": 4, "disturbance": "all connections closed at once, no Deregister"
  },
  "versions": {
    "gateway_probe": "$(git_rev "$PROBE_DIR")", "gateway_probe_dirty": $(git_dirty "$PROBE_DIR"),
    "fleet_emulator": "$(git_rev "$EMU_DIR")", "fleet_emulator_dirty": $(git_dirty "$EMU_DIR"),
    "lwm2m_objects": "$(git_rev "$LIB_DIR")", "experiments": "$(git_rev "$HERE")", "experiments_dirty": $(git_dirty "$HERE"),
    "java": "$("$JAVA" -version 2>&1 | head -1 | sed 's/"/\\"/g')"
  },
  "host": {
    "os": "$(uname -srm)",
    "cpu": "$( (sysctl -n machdep.cpu.brand_string 2>/dev/null || grep -m1 'model name' /proc/cpuinfo | cut -d: -f2) | xargs)",
    "cores": $( (sysctl -n hw.ncpu 2>/dev/null || nproc) ),
    "somaxconn": "$( (sysctl -n kern.ipc.somaxconn 2>/dev/null || cat /proc/sys/net/core/somaxconn) )",
    "emulator_and_probe_on_same_host": true
  }
}
EOF

for rep in $(seq 1 "$REPS"); do
	dir="$OUT/rep$rep"
	mkdir -p "$dir"
	probe="http://localhost:$PROBE_HTTP/actuator/prometheus"
	emu="http://localhost:$EMU_HTTP"
	log "=== repetition $rep"
	"$JAVA" -jar "$PROBE_JAR" --server.port=$PROBE_HTTP \
		--probe.lwm2m.tcp.port=$TCP_PORT --probe.lwm2m.tls.port=$TLS_PORT \
		--probe.lwm2m.tls.key-store="file:$CERTS/server.p12" --probe.lwm2m.tls.trusted-certificates="file:$CERTS/ca.pem" \
		--probe.lwm2m.transport.io-threads=4 \
		--probe.event-log.enabled=true --probe.event-log.directory="$dir/event-log" \
		--management.health.redis.enabled=false > "$dir/probe.log" 2>&1 &
	PIDS+=($!)
	wait_http "http://localhost:$PROBE_HTTP/actuator/health/readiness"
	"$JAVA" -jar "$EMU_JAR" --server.port=$EMU_HTTP \
		--emulator.server.port=$TLS_PORT --emulator.tls.enabled=true \
		--emulator.tls.ca-certificate="$CERTS/ca.pem" --emulator.tls.ca-private-key="$CERTS/ca.key" \
		--emulator.devices.count="$DEVICES" --emulator.devices.endpoint-prefix=t2 \
		--emulator.sensors.per-device="$SENSORS" --emulator.sensors.sample-period=1s \
		--emulator.connect.rate-per-second="$CONNECT_RATE" \
		--emulator.reconnect.strategy="$RECONNECT" --emulator.reconnect.base-delay="$BASE_DELAY" \
		--emulator.reconnect.max-delay="$MAX_DELAY" > "$dir/emulator.log" 2>&1 &
	PIDS+=($!)
	wait_http "$emu/actuator/health"

	curl -sf -XPOST "$emu/fleet/start" >/dev/null
	reg='^probe_lwm2m_registrations_total\{event="registered"'
	obs='^probe_lwm2m_observe_latency_seconds_count\{outcome="success"'
	wait_metric "$probe" "$reg" "$DEVICES"
	wait_metric "$probe" "$obs" $((DEVICES * SENSORS))
	sleep 5
	curl -s "$probe" > "$dir/probe-metrics-before.txt"
	curl -s "$emu/actuator/prometheus" > "$dir/emulator-metrics-before.txt"

	# the disturbance: every connection closed at once, no Deregister
	t_drop=$(now)
	dropped=$(curl -sf -XPOST "$emu/fleet/drop?count=$DEVICES" | python3 -c 'import json,sys;print(json.load(sys.stdin)["dropped"])')
	complete=true
	wait_metric "$probe" "$reg" $((2 * DEVICES)) || complete=false
	wait_metric "$probe" "$obs" $((2 * DEVICES * SENSORS)) || complete=false
	t_done=$(now)
	sleep 3
	curl -s "$probe" > "$dir/probe-metrics-after.txt"
	curl -s "$emu/actuator/prometheus" > "$dir/emulator-metrics-after.txt"
	cat > "$dir/run.json" <<EOF
{ "repetition": $rep, "t_drop": $t_drop, "t_done": $t_done, "dropped": $dropped, "complete": $complete }
EOF
	curl -sf -XPOST "$emu/fleet/stop" >/dev/null || true
	sleep 1
	cleanup
	sleep 2
	log "    dropped $dropped, recovered in $(python3 -c "print(round($t_done - $t_drop, 2))") s (poll resolution 0.2 s), complete=$complete"
done
log "raw results: $OUT"
log "figure:      $HERE/../.venv/bin/python $HERE/plot.py $OUT"
