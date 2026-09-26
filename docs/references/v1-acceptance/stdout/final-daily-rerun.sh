#!/bin/bash
# WHI-1447: daily D1-D4 relaunched with a 12 h wrapper (the first launch's 4 h wrapper
# killed them in the tracemalloc memory pass on the overloaded shared machine).
cd /Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/whi-1447
C=data/corpus/mantle-5src-101082044
L=data/logs/whi-1447
launch() {
  local label=$1 t=$2; shift 2
  ( echo "start $(date -u +%FT%TZ) $*"; timeout $t uv run python main.py run "$@" --results-dir data/results; echo "exit $? end $(date -u +%FT%TZ)" ) > $L/final-$label.log 2>&1 &
}
launch D1-daily_gross-full     43200 --bundle $C/bundle_report     --profile config/daily_gross.yaml
launch D2-daily_gross-cohort   43200 --bundle $C/sor_cohort_report --profile config/daily_gross.yaml
launch D3-daily-full           43200 --bundle $C/bundle_report     --profile config/daily.yaml
launch D4-daily-cohort         43200 --bundle $C/sor_cohort_report --profile config/daily.yaml
wait
