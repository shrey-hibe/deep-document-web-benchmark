#!/bin/zsh
# Open the warehouse in DuckDB's UI, in Chrome, read only.
# Stop it with: pkill -f "start_ui_server"
REPO="${0:A:h}"
ATTACH="ATTACH '$REPO/data/warehouse.duckdb' AS ddwb (READ_ONLY);"
if lsof -iTCP:4213 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "A DuckDB UI is already running. Restart it with: pkill -f start_ui_server && $0"
  open -a "Google Chrome" http://localhost:4213/; exit 0
fi
( tail -f /dev/null | duckdb -cmd "$ATTACH USE ddwb; CALL start_ui_server();" >/dev/null 2>&1 & )
for i in 1 2 3 4 5 6 7 8 9 10; do lsof -iTCP:4213 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 0.5; done
open -a "Google Chrome" http://localhost:4213/
