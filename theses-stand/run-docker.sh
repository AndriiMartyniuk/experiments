#!/usr/bin/env bash
# Theses stand, inside Docker: gateway-probe and fleet-emulator as containers on one Docker network
# (../stand/docker-compose.yml), i.e. on the Linux network stack of the Docker VM with raised listen limits.
# Same scenario as run.sh: DEVICES devices with SENSORS Probe Sensors connect over CoAP over TLS, register and are
# observed; then ALL connections are closed at once (no Deregister) and the devices reconnect with RECONNECT.
#
# Parameters (environment variables):
#   DEVICES=5000  SENSORS=2  REPS=3  CONNECT_RATE=500
#   RECONNECT=DECORRELATED_JITTER  BASE_DELAY=200ms  MAX_DELAY=5s
#   STAND_SOMAXCONN=4096  STAND_SYN_BACKLOG=4096  PROBE_BACKLOG=4096  PROBE_IO_THREADS=4  PROBE_DISPATCH=SINGLE
#   EMULATOR_IO_THREADS=0 (= 2 x CPUs)  PROBE_COAP_MESSAGES=false (log every CoAP message: phase decomposition)
#   PROBE_JAVA_OPTS=-Xmx1g  EMULATOR_JAVA_OPTS=-Xmx2g  RUN_ID=<timestamp>  SKIP_BUILD=0  SAMPLE_PERIOD_S=0.1
# During each disturbance sample.py records CPU of both processes and the probe's dispatch queue (samples.csv).
# Figure and numbers: ../.venv/bin/python plot.py raw/<run-id>
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="$(cd "$HERE/../.." && pwd)"
STAND="$HERE/../stand/docker-compose.yml"

export DEVICES=${DEVICES:-5000}
export SENSORS=${SENSORS:-2}
REPS=${REPS:-3}
export CONNECT_RATE=${CONNECT_RATE:-500}
export RECONNECT=${RECONNECT:-DECORRELATED_JITTER}
export BASE_DELAY=${BASE_DELAY:-200ms}
export MAX_DELAY=${MAX_DELAY:-5s}
export STAND_SOMAXCONN=${STAND_SOMAXCONN:-4096}
export STAND_SYN_BACKLOG=${STAND_SYN_BACKLOG:-4096}
export PROBE_BACKLOG=${PROBE_BACKLOG:-4096}
export PROBE_IO_THREADS=${PROBE_IO_THREADS:-4}
export PROBE_DISPATCH=${PROBE_DISPATCH:-SINGLE}
export EMULATOR_IO_THREADS=${EMULATOR_IO_THREADS:-0}
export PROBE_COAP_MESSAGES=${PROBE_COAP_MESSAGES:-false}
SAMPLE_PERIOD_S=${SAMPLE_PERIOD_S:-0.1}
export PROBE_JAVA_OPTS=${PROBE_JAVA_OPTS:--Xmx1g}
export EMULATOR_JAVA_OPTS=${EMULATOR_JAVA_OPTS:--Xmx2g}
RUN_ID=${RUN_ID:-$(date +%Y%m%d-%H%M%S)-docker}
SKIP_BUILD=${SKIP_BUILD:-0}
WAIT_TIMEOUT=${WAIT_TIMEOUT:-120}

PROBE="http://localhost:18080/actuator/prometheus"
EMU="http://localhost:18090"
OUT="$HERE/raw/$RUN_ID"
mkdir -p "$OUT"
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
compose() { docker compose -f "$STAND" "$@"; }
trap 'compose down -t 5 >/dev/null 2>&1 || true' EXIT
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
now() { python3 -c 'import time;print(time.time())'; }

if [[ "$SKIP_BUILD" != 1 ]]; then
	log "building images (lwm2m-objects -> ~/.m2, then compose build)"
	(cd "$CODE/lwm2m-objects" && JAVA_HOME=$(/usr/libexec/java_home -v 25 2>/dev/null || echo "$JAVA_HOME") ./gradlew -q publishToMavenLocal)
	compose build -q
fi

git_rev() { git -C "$1" rev-parse --short HEAD 2>/dev/null || echo unknown; }
git_dirty() { [[ -n "$(git -C "$1" status --porcelain 2>/dev/null)" ]] && echo true || echo false; }
DOCKER_INFO=$(docker info --format '{{.NCPU}}|{{.MemTotal}}|{{.KernelVersion}}|{{.OperatingSystem}}')
IFS='|' read -r VM_CPUS VM_MEM VM_KERNEL VM_OS <<< "$DOCKER_INFO"
OTHER_CONTAINERS=$(docker ps -q | wc -l | tr -d ' ')

for rep in $(seq 1 "$REPS"); do
	dir="$OUT/rep$rep"
	mkdir -p "$dir/event-log"
	export STAND_EVENT_LOG_DIR="$dir/event-log"
	log "=== repetition $rep: $DEVICES devices"
	compose up -d --force-recreate --wait >/dev/null
	if [[ $rep == 1 ]]; then
		LIMITS=$(compose exec -T probe sh -c 'echo "$(cat /proc/sys/net/core/somaxconn)|$(cat /proc/sys/net/ipv4/tcp_max_syn_backlog)|$(ulimit -n)"')
		IFS='|' read -r C_SOMAXCONN C_SYN C_NOFILE <<< "$LIMITS"
	fi

	curl -sf -XPOST "$EMU/fleet/start" >/dev/null
	reg='^probe_lwm2m_registrations_total\{event="registered"'
	obs='^probe_lwm2m_observe_latency_seconds_count\{outcome="success"'
	# a degraded run is a result, not an error: record it and go on
	warmup_complete=true
	wait_metric "$PROBE" "$reg" "$DEVICES" || warmup_complete=false
	wait_metric "$PROBE" "$obs" $((DEVICES * SENSORS)) || warmup_complete=false
	sleep 5
	curl -s "$PROBE" > "$dir/probe-metrics-before.txt"
	curl -s "$EMU/actuator/prometheus" > "$dir/emulator-metrics-before.txt"

	# counters BEFORE the drop: devices start re-registering within milliseconds of it
	reg_before=$(metric "$PROBE" "$reg"); obs_before=$(metric "$PROBE" "$obs")
	python3 "$HERE/sample.py" "$PROBE" "$EMU/actuator/prometheus" "$SAMPLE_PERIOD_S" > "$dir/samples.csv" 2> "$dir/samples.err" &
	sampler=$!
	sleep 1
	t_drop=$(now)
	dropped=$(curl -sf -XPOST "$EMU/fleet/drop?count=$DEVICES" | python3 -c 'import json,sys;print(json.load(sys.stdin)["dropped"])')
	complete=true
	wait_metric "$PROBE" "$reg" $((reg_before + dropped)) || complete=false
	wait_metric "$PROBE" "$obs" $((obs_before + dropped * SENSORS)) || complete=false
	t_done=$(now)
	sleep 3
	kill "$sampler" 2>/dev/null || true; wait "$sampler" 2>/dev/null || true
	curl -s "$PROBE" > "$dir/probe-metrics-after.txt"
	curl -s "$EMU/actuator/prometheus" > "$dir/emulator-metrics-after.txt"
	docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}' \
		$(compose ps -q) > "$dir/docker-stats.txt" 2>/dev/null || true
	cat > "$dir/run.json" <<EOF
{ "repetition": $rep, "t_drop": $t_drop, "t_done": $t_done, "dropped": $dropped, "complete": $complete,
  "warmup_complete": $warmup_complete,
  "clock": "host t_drop; probe event log in container" }
EOF
	curl -sf -XPOST "$EMU/fleet/stop" >/dev/null || true
	compose logs --no-color probe > "$dir/probe.log" 2>&1 || true
	compose logs --no-color emulator > "$dir/emulator.log" 2>&1 || true
	compose down -t 5 >/dev/null
	log "    dropped $dropped, recovered in $(python3 -c "print(round($t_done - $t_drop, 2))") s (poll resolution 0.2 s), complete=$complete"
done

cat > "$OUT/environment.json" <<EOF
{
  "run_id": "$RUN_ID",
  "finished": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "parameters": {
    "devices": $DEVICES, "sensors": $SENSORS, "repetitions": $REPS, "connect_rate": $CONNECT_RATE,
    "reconnect_strategy": "$RECONNECT", "base_delay": "$BASE_DELAY", "max_delay": "$MAX_DELAY",
    "transport": "coaps+tcp (TLS 1.2, mutual X.509, session resumption on)", "sample_period": "1s",
    "placement": "IN_MEMORY", "dispatch": "$PROBE_DISPATCH", "io_threads": $PROBE_IO_THREADS,
    "emulator_io_threads": "$EMULATOR_IO_THREADS (0 = 2 x CPUs)", "coap_message_log": $PROBE_COAP_MESSAGES,
    "probe_observe_timeout": "10s", "probe_tls_handshake_timeout": "10s", "emulator_registration_timeout": "10s",
    "emulator_tls_handshake_timeout": "10s", "emulator_connect_timeout": "5s", "sample_period_s": $SAMPLE_PERIOD_S,
    "probe_backlog": $PROBE_BACKLOG, "probe_java_opts": "$PROBE_JAVA_OPTS", "emulator_java_opts": "$EMULATOR_JAVA_OPTS",
    "disturbance": "all connections closed at once, no Deregister"
  },
  "versions": {
    "gateway_probe": "$(git_rev "$CODE/gateway-probe")", "gateway_probe_dirty": $(git_dirty "$CODE/gateway-probe"),
    "fleet_emulator": "$(git_rev "$CODE/fleet-emulator")", "fleet_emulator_dirty": $(git_dirty "$CODE/fleet-emulator"),
    "lwm2m_objects": "$(git_rev "$CODE/lwm2m-objects")", "experiments": "$(git_rev "$HERE")", "experiments_dirty": $(git_dirty "$HERE"),
    "image_jre": "eclipse-temurin:25-jre"
  },
  "host": {
    "os": "$(uname -srm)",
    "cpu": "$(sysctl -n machdep.cpu.brand_string 2>/dev/null || grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)",
    "runtime": "docker: $VM_OS, kernel $VM_KERNEL, $VM_CPUS CPUs, $VM_MEM bytes memory for the VM",
    "somaxconn": "$C_SOMAXCONN", "tcp_max_syn_backlog": "$C_SYN", "nofile": "$C_NOFILE",
    "other_containers_running_in_vm": $OTHER_CONTAINERS,
    "emulator_and_probe_on_same_host": true, "network": "docker bridge network (probe and emulator containers)"
  }
}
EOF
log "raw results: $OUT"
log "figure:      $HERE/../.venv/bin/python $HERE/plot.py $OUT"
