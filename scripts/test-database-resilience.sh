#!/bin/bash

# Operator-driven database outage and recovery rehearsal.

set -e

echo "🧪 Database Resilience Test Script"
echo "=================================="
echo ""
echo "This script will simulate database outages to test resilience features."
echo "Make sure all services are running before starting."
echo ""
read -r -p "Press Enter to continue or Ctrl+C to cancel..."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

check_health() {
  local service=$1
  local port=$2
  local url="http://localhost:${port}/health"

  if curl -s "$url" >/dev/null 2>&1; then
    echo -e "${GREEN}✅ ${service} is healthy${NC}"
    return 0
  else
    echo -e "${RED}❌ ${service} is not responding${NC}"
    return 1
  fi
}

get_queue_depth() {
  local queue=$1
  local depth
  depth=$(docker exec groovemap-rabbitmq rabbitmqctl list_queues name messages | grep "$queue" | awk '{print $2}' 2>/dev/null || echo "0")
  echo "$depth"
}

report_queue_depths() {
  local data_type graphinator_queue tableinator_queue g_depth t_depth
  for data_type in artists labels masters releases; do
    graphinator_queue="groovemap-discogs-graphinator-${data_type}"
    tableinator_queue="groovemap-discogs-tableinator-${data_type}"
    g_depth=$(get_queue_depth "$graphinator_queue")
    t_depth=$(get_queue_depth "$tableinator_queue")
    echo "Queue ${data_type}: graphinator=${g_depth:-0}, tableinator=${t_depth:-0}"
  done
}

simulate_outage() {
  local service=$1
  local duration=$2

  echo -e "\n${YELLOW}🔌 Stopping ${service} for ${duration} seconds...${NC}"
  docker compose stop "$service"

  echo -e "${BLUE}⏳ Waiting ${duration} seconds...${NC}"
  sleep "$duration"

  echo -e "${GREEN}🔄 Restarting ${service}...${NC}"
  docker compose start "$service"

  echo -e "${BLUE}⏳ Waiting for ${service} to be ready...${NC}"
  sleep 10
}

monitor_logs() {
  local service=$1
  local duration=$2

  echo -e "\n${BLUE}📋 Monitoring ${service} logs for ${duration} seconds...${NC}"
  timeout "$duration" docker compose logs -f "$service" 2>&1 | grep -E "(Circuit breaker|Retrying|connection|Connection|Failed|failed|established|resilient)" || true
}

echo -e "\n${BLUE}🏥 Initial Health Check${NC}"
echo "========================"
check_health "Dashboard" 8003
check_health "API" 8004

echo -e "\n${BLUE}📊 Initial Queue Depths${NC}"
echo "======================="
report_queue_depths

echo -e "\n${YELLOW}🧪 Test 1: Neo4j Outage (30 seconds)${NC}"
echo "====================================="
echo "Simulating Neo4j maintenance window..."

monitor_logs "graphinator" 60 &
MONITOR_PID=$!

simulate_outage "neo4j" 30

kill "$MONITOR_PID" 2>/dev/null || true

echo -e "\n${BLUE}🔍 Checking Neo4j Recovery${NC}"
sleep 5
check_health "Graphinator" 8001

echo -e "\n${YELLOW}🧪 Test 2: PostgreSQL Outage (30 seconds)${NC}"
echo "=========================================="
echo "Simulating PostgreSQL maintenance window..."

monitor_logs "tableinator" 60 &
MONITOR_PID=$!

simulate_outage "postgres" 30

kill "$MONITOR_PID" 2>/dev/null || true

echo -e "\n${BLUE}🔍 Checking PostgreSQL Recovery${NC}"
sleep 5
check_health "Tableinator" 8002

echo -e "\n${YELLOW}🧪 Test 3: RabbitMQ Outage (20 seconds)${NC}"
echo "========================================"
echo "Simulating RabbitMQ maintenance window..."
echo -e "${RED}⚠️  This is more disruptive as it affects message flow${NC}"

MONITOR_PIDS=()
for service in extractor-discogs extractor-musicbrainz graphinator tableinator; do
  monitor_logs "$service" 50 &
  MONITOR_PIDS+=($!)
done

simulate_outage "rabbitmq" 20

sleep 30
for pid in "${MONITOR_PIDS[@]}"; do
  kill "$pid" 2>/dev/null || true
done

echo -e "\n${BLUE}🔍 Checking RabbitMQ Recovery${NC}"
sleep 10
echo -e "\n${BLUE}🏥 Final Health Check${NC}"
echo "====================="
check_health "Dashboard" 8003
check_health "API" 8004

echo -e "\n${BLUE}📊 Final Queue Depths${NC}"
echo "===================="
report_queue_depths

echo -e "\n${GREEN}✅ Resilience tests completed!${NC}"
echo ""
echo "Review the logs above to verify:"
echo "1. Circuit breakers activated during outages"
echo "2. Services attempted reconnection with exponential backoff"
echo "3. Services recovered after databases restarted"
echo "4. No messages were lost (check queue depths)"
echo ""
echo "For more detailed analysis, check individual service logs:"
echo "  docker compose logs graphinator | grep -i circuit"
echo "  docker compose logs tableinator | grep -i retry"
echo "  docker compose logs extractor-discogs extractor-musicbrainz | grep -i connection"
