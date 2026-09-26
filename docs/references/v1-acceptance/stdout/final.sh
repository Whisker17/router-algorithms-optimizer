#!/bin/bash
# WHI-1447 final acceptance runs (held-out report split), launched concurrently.
cd /Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/whi-1447
C=data/corpus/mantle-5src-101082044
L=data/logs/whi-1447
launch() { # label timeout args...
  local label=$1 t=$2; shift 2
  ( echo "start $(date -u +%FT%TZ) $*"; timeout $t uv run python main.py run "$@" --results-dir data/results; echo "exit $? end $(date -u +%FT%TZ)" ) > $L/final-$label.log 2>&1 &
}
launch F1-full_gross-full      43200 --bundle $C/bundle_report     --profile config/full_gross.yaml
launch F2-full_gross-cohort    43200 --bundle $C/sor_cohort_report --profile config/full_gross.yaml
launch F3-full-full            43200 --bundle $C/bundle_report     --profile config/full.yaml
launch F4-full-cohort          43200 --bundle $C/sor_cohort_report --profile config/full.yaml
launch F5-full_gross-full-rev  43200 --bundle $C/bundle_report     --profile config/full_gross.yaml --order reverse
launch D1-daily_gross-full     14400 --bundle $C/bundle_report     --profile config/daily_gross.yaml
launch D2-daily_gross-cohort   14400 --bundle $C/sor_cohort_report --profile config/daily_gross.yaml
launch D3-daily-full           14400 --bundle $C/bundle_report     --profile config/daily.yaml
launch D4-daily-cohort         14400 --bundle $C/sor_cohort_report --profile config/daily.yaml
wait
