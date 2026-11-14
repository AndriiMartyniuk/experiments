#!/usr/bin/env bash
# E1 — recovery cost of one device session, per component:
#   TLS handshake (full vs resumed), CSM + Register, re-establishing m observations.
#
# For every configuration (m sensors x TLS session resumption on/off) a fresh gateway-probe and fleet-emulator are
# started; DEVICES devices register (warm-up wave), then their connections are dropped in small batches so that each
# reconnect is an isolated session (no contention). The probe's per-session event log, metric snapshots and process
# CPU time are stored under raw/<run-id>/<config>/. Analyse with: python3 analyze.py raw/<run-id>
#
# Parameters (environment variables):
#   DEVICES=20           devices per configuration
#   WARMUP_CYCLES=2      unmeasured reconnects per device after the first wave (JIT warm-up of the reconnect path)
#   CYCLES=5             measured reconnects per device (sessions measured = DEVICES * CYCLES)
#   SENSORS="0 1 2 5"    values of m (Probe Sensor instances = observations per device)
#   RESUMPTION="true false"
#   DROP_BATCH=2         connections dropped at a time
#   DROP_INTERVAL=0.5    seconds between drop batches
#   CONNECT_RATE=10      devices started per second in the warm-up wave
#   IDLE_WINDOW=5        seconds of idle CPU measured before the reconnects (baseline)
#   COAP_MESSAGE_LOG=true  log every CoAP message (per-phase bytes per session); adds probe CPU (JSON per message)
#   PLACEMENT=IN_MEMORY  probe.state.placement (IN_MEMORY | REDIS | REDIS_CACHED; Redis via gateway-probe compose)
#   DISPATCH=SINGLE      probe.lwm2m.transport.dispatch (SINGLE | VIRTUAL)
#   RUN_ID=<timestamp>   output directory name under raw/
#   SKIP_BUILD=0         1 = use the existing jars
#   E1_JAVA_HOME         Java 25+ runtime (default: /usr/libexec/java_home -v 25 on macOS, else JAVA_HOME)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="$(cd "$HERE/../.." && pwd)"
PROBE_DIR="$CODE/gateway-probe"
EMU_DIR="$CODE/fleet-emulator"
LIB_DIR="$CODE/lwm2m-objects"

DEVICES=${DEVICES:-20}
WARMUP_CYCLES=${WARMUP_CYCLES:-2}
CYCLES=${CYCLES:-5}
SENSORS=${SENSORS:-"0 1 2 5"}
RESUMPTION=${RESUMPTION:-"true false"}
DROP_BATCH=${DROP_BATCH:-2}
DROP_INTERVAL=${DROP_INTERVAL:-0.5}
CONNECT_RATE=${CONNECT_RATE:-10}
IDLE_WINDOW=${IDLE_WINDOW:-5}
COAP_MESSAGE_LOG=${COAP_MESSAGE_LOG:-true}
PLACEMENT=${PLACEMENT:-IN_MEMORY}
DISPATCH=${DISPATCH:-SINGLE}
RUN_ID=${RUN_ID:-$(date +%Y%m%d-%H%M%S)}
SKIP_BUILD=${SKIP_BUILD:-0}

PROBE_HTTP=18080
EMU_HTTP=18090
TLS_PORT=15684
TCP_PORT=15683
WAIT_TIMEOUT=120

# Java 25 runtime: E1_JAVA_HOME if set, otherwise the macOS java_home lookup, otherwise JAVA_HOME
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
	for pid in "${PIDS[@]:-}"; do
		[[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
	done
	PIDS=()
}
trap cleanup EXIT

# sum of all samples of a Prometheus metric matching a regex (0 if absent)
metric() {
	curl -s "$1" | awk -v re="$2" '$0 ~ re && $0 !~ /^#/ { s += $NF } END { printf "%.0f\n", s + 0 }'
}

# wait until metric >= target; returns 1 on timeout
wait_metric() {
	local url=$1 re=$2 target=$3 deadline=$((SECONDS + WAIT_TIMEOUT))
	while (( SECONDS < deadline )); do
		local value
		value=$(metric "$url" "$re")
		(( value >= target )) && return 0
		sleep 0.2
	done
	log "timeout waiting for $re >= $target (last: $(metric "$url" "$re"))"
	return 1
}

wait_http() {
	local url=$1 deadline=$((SECONDS + WAIT_TIMEOUT))
	until curl -sf "$url" >/dev/null; do
		(( SECONDS < deadline )) || { log "timeout waiting for $url"; return 1; }
		sleep 0.3
	done
}

# ---- build -------------------------------------------------------------------------------------------------------
if [[ "$SKIP_BUILD" != 1 ]]; then
	log "building lwm2m-objects, gateway-probe, fleet-emulator"
	(cd "$LIB_DIR" && ./gradlew -q publishToMavenLocal)
	(cd "$PROBE_DIR" && ./gradlew -q bootJar)
	(cd "$EMU_DIR" && ./gradlew -q bootJar)
fi
PROBE_JAR=$(ls "$PROBE_DIR"/build/libs/gateway-probe-*-SNAPSHOT.jar | grep -v plain | head -1)
EMU_JAR=$(ls "$EMU_DIR"/build/libs/fleet-emulator-*-SNAPSHOT.jar | grep -v plain | head -1)

CERTS="$PROBE_DIR/certs"
[[ -f "$CERTS/ca.key" && -f "$CERTS/server.p12" ]] || (cd "$PROBE_DIR" && scripts/generate-dev-certs.sh >/dev/null)

REDIS_ARGS=()
if [[ "$PLACEMENT" == REDIS* ]]; then
	(cd "$PROBE_DIR" && REDIS_HOST_PORT=16379 docker compose up -d --wait redis >/dev/null)
	REDIS_ARGS=(--spring.data.redis.port=16379)
fi

# ---- environment record ------------------------------------------------------------------------------------------
git_rev() { git -C "$1" rev-parse --short HEAD 2>/dev/null || echo unknown; }
git_dirty() { [[ -n "$(git -C "$1" status --porcelain 2>/dev/null)" ]] && echo true || echo false; }
cat > "$OUT/environment.json" <<EOF
{
  "run_id": "$RUN_ID",
  "started": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "parameters": {
    "devices": $DEVICES, "warmup_cycles": $WARMUP_CYCLES, "cycles": $CYCLES, "sensors": "$SENSORS", "resumption": "$RESUMPTION",
    "drop_batch": $DROP_BATCH, "drop_interval_s": $DROP_INTERVAL, "connect_rate": $CONNECT_RATE,
    "idle_window_s": $IDLE_WINDOW, "coap_message_log": $COAP_MESSAGE_LOG, "placement": "$PLACEMENT", "dispatch": "$DISPATCH",
    "transport": "coaps+tcp", "reconnect_strategy": "IMMEDIATE", "sample_period": "0s"
  },
  "versions": {
    "gateway_probe": "$(git_rev "$PROBE_DIR")", "gateway_probe_dirty": $(git_dirty "$PROBE_DIR"),
    "fleet_emulator": "$(git_rev "$EMU_DIR")", "fleet_emulator_dirty": $(git_dirty "$EMU_DIR"),
    "lwm2m_objects": "$(git_rev "$LIB_DIR")", "lwm2m_objects_dirty": $(git_dirty "$LIB_DIR"),
    "experiments": "$(git_rev "$HERE")", "experiments_dirty": $(git_dirty "$HERE"),
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

# drop COUNT complete sessions in batches, then wait until all of them are recovered
# (expects: emu url, probe url, total registrations and observations to wait for)
drop_and_wait() {
	local emu=$1 probe=$2 count=$3 expected_registrations=$4 m=$5 dropped=0
	while (( dropped < count )); do
		local batch=$DROP_BATCH
		(( dropped + batch > count )) && batch=$((count - dropped))
		local n
		n=$(curl -sf -XPOST "$emu/fleet/drop?count=$batch&complete=true" | python3 -c 'import json,sys;print(json.load(sys.stdin)["dropped"])')
		dropped=$((dropped + n))
		sleep "$DROP_INTERVAL"
	done
	local ok=0
	wait_metric "$probe" '^probe_lwm2m_registrations_total\{event="registered"' "$expected_registrations" || ok=1
	wait_metric "$probe" '^probe_lwm2m_observe_latency_seconds_count\{outcome="success"' $((expected_registrations * m)) || ok=1
	return $ok
}

# ---- one configuration -------------------------------------------------------------------------------------------
run_config() {
	local m=$1 resumption=$2
	local name="m${m}-resumption-${resumption}"
	local dir="$OUT/$name"
	local probe="http://localhost:$PROBE_HTTP/actuator/prometheus"
	local emu="http://localhost:$EMU_HTTP"
	mkdir -p "$dir"
	log "=== $name"

	"$JAVA" -jar "$PROBE_JAR" \
		--server.port=$PROBE_HTTP \
		--probe.lwm2m.tcp.port=$TCP_PORT --probe.lwm2m.tls.port=$TLS_PORT \
		--probe.lwm2m.tls.key-store="file:$CERTS/server.p12" \
		--probe.lwm2m.tls.trusted-certificates="file:$CERTS/ca.pem" \
		--probe.state.placement="$PLACEMENT" --probe.lwm2m.transport.dispatch="$DISPATCH" \
		--probe.event-log.enabled=true --probe.event-log.coap-messages="$COAP_MESSAGE_LOG" \
		--probe.event-log.directory="$dir/event-log" \
		--management.health.redis.enabled=false ${REDIS_ARGS[@]+"${REDIS_ARGS[@]}"} \
		> "$dir/probe.log" 2>&1 &
	PIDS+=($!)
	wait_http "http://localhost:$PROBE_HTTP/actuator/health/readiness"

	"$JAVA" -jar "$EMU_JAR" \
		--server.port=$EMU_HTTP \
		--emulator.server.port=$TLS_PORT \
		--emulator.tls.enabled=true \
		--emulator.tls.ca-certificate="$CERTS/ca.pem" --emulator.tls.ca-private-key="$CERTS/ca.key" \
		--emulator.tls.session-resumption="$resumption" \
		--emulator.devices.count="$DEVICES" --emulator.devices.endpoint-prefix="e1" \
		--emulator.sensors.per-device="$m" --emulator.sensors.sample-period=0s \
		--emulator.connect.rate-per-second="$CONNECT_RATE" \
		--emulator.reconnect.strategy=IMMEDIATE \
		> "$dir/emulator.log" 2>&1 &
	PIDS+=($!)
	wait_http "$emu/actuator/health"

	# warm-up wave: every device connects (full handshake), registers and is observed
	curl -sf -XPOST "$emu/fleet/start" >/dev/null
	local registered_re='^probe_lwm2m_registrations_total\{event="registered"'
	local observed_re='^probe_lwm2m_observe_latency_seconds_count\{outcome="success"'
	wait_metric "$probe" "$registered_re" "$DEVICES" || true
	wait_metric "$probe" "$observed_re" $((DEVICES * m)) || true
	# warm-up reconnects (not measured): JIT-compile the reconnect / resumption path
	local warmups=$((DEVICES * WARMUP_CYCLES))
	(( warmups > 0 )) && { drop_and_wait "$emu" "$probe" "$warmups" $((DEVICES + warmups)) "$m" || true; }
	curl -s "$probe" > "$dir/probe-metrics-warmup.txt"

	# idle baseline
	local cpu_idle_start cpu_idle_end cpu_end t_idle_start t_cycles_start t_cycles_end
	sleep 1
	t_idle_start=$(python3 -c 'import time;print(time.time())')
	cpu_idle_start=$(metric "$probe" '^process_cpu_time_ns_total')
	sleep "$IDLE_WINDOW"
	cpu_idle_end=$(metric "$probe" '^process_cpu_time_ns_total')
	t_cycles_start=$(python3 -c 'import time;print(time.time())')

	# measured reconnects: drop in small batches, devices reconnect immediately
	local drops=$((DEVICES * CYCLES)) complete=true
	drop_and_wait "$emu" "$probe" "$drops" $((DEVICES + warmups + drops)) "$m" || complete=false
	t_cycles_end=$(python3 -c 'import time;print(time.time())')
	cpu_end=$(metric "$probe" '^process_cpu_time_ns_total')

	curl -s "$probe" > "$dir/probe-metrics.txt"
	curl -s "$emu/actuator/prometheus" > "$dir/emulator-metrics.txt"
	cat > "$dir/cpu.json" <<EOF
{
  "m": $m, "resumption": $resumption, "devices": $DEVICES, "reconnects": $drops, "complete": $complete,
  "t_idle_start": $t_idle_start, "t_reconnects_start": $t_cycles_start, "t_reconnects_end": $t_cycles_end,
  "idle_window_s": $(python3 -c "print($t_cycles_start - $t_idle_start)"),
  "cpu_idle_ns": $((cpu_idle_end - cpu_idle_start)),
  "reconnect_window_s": $(python3 -c "print($t_cycles_end - $t_cycles_start)"),
  "cpu_reconnect_ns": $((cpu_end - cpu_idle_end))
}
EOF
	curl -sf -XPOST "$emu/fleet/stop" >/dev/null || true
	sleep 1
	cleanup
	sleep 1
	log "    sessions: $drops reconnects, complete=$complete"
}

for m in $SENSORS; do
	for resumption in $RESUMPTION; do
		run_config "$m" "$resumption"
	done
done

if [[ "$PLACEMENT" == REDIS* ]]; then
	(cd "$PROBE_DIR" && docker compose down >/dev/null 2>&1) || true
fi
log "raw results: $OUT"
log "analyse:     python3 $HERE/analyze.py $OUT"
