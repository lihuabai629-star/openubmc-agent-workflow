#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: RUN_DIR=/tmp/openubmc-build PREFIX=product ./run_bmcgo_background.sh -- <command> [args...]

Start a long bmcgo command in a detached background runner and write:
  <prefix>-<stamp>.pid   background shell pid
  <prefix>-<stamp>.log   combined stdout/stderr
  <prefix>-<stamp>.rc    command exit code, only after clean wrapper completion
  <prefix>-<stamp>.meta  cwd, command, start time, and artifact paths
  <prefix>-<stamp>.runner.sh  generated runner script

If the pid exits but the .rc file is missing, treat the build as interrupted or incomplete.
HELP
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

if [ "${1:-}" != "--" ]; then
  usage >&2
  exit 2
fi
shift

if [ "$#" -eq 0 ]; then
  printf 'run_bmcgo_background.sh: missing command after --\n' >&2
  exit 2
fi

run_dir="${RUN_DIR:-/tmp/openubmc-bmcgo-run}"
prefix="${PREFIX:-bmcgo}"
stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$run_dir"

log="$run_dir/${prefix}-${stamp}.log"
pid_file="$run_dir/${prefix}-${stamp}.pid"
rc_file="$run_dir/${prefix}-${stamp}.rc"
meta_file="$run_dir/${prefix}-${stamp}.meta"
runner_file="$run_dir/${prefix}-${stamp}.runner.sh"

{
  printf 'start_epoch=%s\n' "$(date +%s)"
  printf 'start_time=%s\n' "$(date -Is)"
  printf 'cwd=%s\n' "$PWD"
  printf 'log=%s\n' "$log"
  printf 'pid_file=%s\n' "$pid_file"
  printf 'rc_file=%s\n' "$rc_file"
  printf 'runner_file=%s\n' "$runner_file"
  printf 'cmd='
  printf '%q ' "$@"
  printf '\n'
} > "$meta_file"

{
  printf '#!/usr/bin/env bash\n'
  printf 'set +e\n'
  printf 'set -o pipefail\n'
  printf 'cd %q || exit $?\n' "$PWD"
  printf 'rc_file=%q\n' "$rc_file"
  printf 'cmd=('
  printf ' %q' "$@"
  printf ' )\n'
  cat <<'RUNNER'
printf '[bmcgo-bg] start %s\n' "$(date -Is)"
printf '[bmcgo-bg] cwd %s\n' "$PWD"
printf '[bmcgo-bg] cmd'
printf ' %q' "${cmd[@]}"
printf '\n'
"${cmd[@]}"
cmd_rc=$?
printf '[bmcgo-bg] finish %s rc=%s\n' "$(date -Is)" "$cmd_rc"
printf '%s\n' "$cmd_rc" > "$rc_file"
exit "$cmd_rc"
RUNNER
} > "$runner_file"
chmod +x "$runner_file"

nohup bash "$runner_file" > "$log" 2>&1 < /dev/null &

bg_pid=$!
printf '%s\n' "$bg_pid" > "$pid_file"

printf 'pid=%s\n' "$bg_pid"
printf 'log=%s\n' "$log"
printf 'rc_file=%s\n' "$rc_file"
printf 'meta=%s\n' "$meta_file"
printf 'runner=%s\n' "$runner_file"
