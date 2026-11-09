#!/usr/bin/env bash
# Runs every test suite against a fresh local database and fails if any fails.
# Used by GitHub on every push (.github/workflows/tests.yml) and locally:
#   ./run_tests.sh            (uses python3.13 if present, else python3)
set -u
cd "$(dirname "$0")"
PY="${PYTHON:-$(command -v python3.13 || command -v python3)}"
unset DATABASE_URL
rm -f instance/pharma_final.db
SUITES="tests_regression tests_csrf tests_ui_sweep tests_privacy tests_checklist
tests_checklist_order tests_scoring tests_palette tests_deploy_config tests_multitask_bonus
tests_delete_fk tests_schema_reconcile tests_checker_pending tests_multitask_report
tests_admin_tabs tests_owner_list tests_error_log tests_levels tests_email_suggest tests_picker_v2 tests_picker_attack tests_push tests_staff_today"
total=0; failed=0; bad=""
for t in $SUITES; do
  out="$("$PY" "$t.py" 2>&1)"
  line="$(printf '%s\n' "$out" | grep -E '^PASSED' | tail -1)"
  p=$(echo "$line" | awk '{print $2}'); f=$(echo "$line" | awk '{print $4}')
  if [ -z "$line" ]; then f=1; p=0; echo "CRASHED  $t"; printf '%s\n' "$out" | tail -15
  elif [ "$f" != "0" ]; then echo "FAILED   $t: $line"; printf '%s\n' "$out" | grep '^FAIL'
  else echo "ok       $t ($p)"; fi
  total=$((total + ${p:-0})); failed=$((failed + ${f:-1}))
  [ "${f:-1}" != "0" ] && bad="$bad $t"
done
echo "TOTAL PASSED $total  FAILED $failed"
[ "$failed" -eq 0 ] || { echo "Failing suites:$bad"; exit 1; }
