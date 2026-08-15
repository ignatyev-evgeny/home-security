#!/usr/bin/env bash
# Прогон всех наборов. Каждый файл — самостоятельный скрипт без фреймворка:
# печатает, что именно проверил, и падает на первом же несоответствии.
#
#   ./tests/run.sh
#
# Зависимости — те же, что у сервисов:
#   pip install -r guard/requirements.txt -r watchdog/requirements.txt

set -u
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

failed=0
for t in tests/test_*.py; do
    printf '%-28s ' "$(basename "$t")"
    if out=$("$PYTHON" "$t" 2>&1); then
        echo "${out##*$'\n'}"
    else
        echo "ПРОВАЛ"
        echo "$out" | sed 's/^/    /'
        failed=$((failed + 1))
    fi
done

echo
if [ "$failed" -eq 0 ]; then
    echo "все наборы прошли"
else
    echo "провалено наборов: $failed"
fi
exit "$failed"
