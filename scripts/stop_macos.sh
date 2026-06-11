#!/bin/bash
# Stop all Xiaomi Miloco services

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

print_ok() { echo -e "${GREEN}✅ $*${NC}"; }

killed=0
for port in 8001 8000 5173; do
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "$pids" | xargs kill -9 2>/dev/null
        print_ok "Stopped service on port $port"
        ((killed++))
    fi
done

if [[ $killed -eq 0 ]]; then
    echo "No running services found."
else
    print_ok "Stopped $killed service(s)"
fi
