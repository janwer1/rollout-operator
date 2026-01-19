#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

DEMO_IMAGE="janwer/demo-app:24h"
NAMESPACE="demo-sts"
STS_NAME="demo-sts"
CLUSTER_NAME="rollout-operator-test"

echo -e "${GREEN}=== Testing Rollout Operator ===${NC}"

# Check if cluster exists
if ! kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
    echo -e "${RED}Error: Cluster ${CLUSTER_NAME} does not exist.${NC}"
    echo -e "${YELLOW}Run $SCRIPT_DIR/run-tests.sh first to create the cluster.${NC}"
    exit 1
fi

# Get current build time from running pods
echo -e "${BLUE}Checking current app version...${NC}"
CURRENT_BUILD=$(kubectl get pods -n "$NAMESPACE" -l app=demo-sts -o jsonpath='{.items[0].spec.containers[0].env[?(@.name=="BUILD_TIME")].value}' 2>/dev/null || echo "unknown")
if [ -n "$CURRENT_BUILD" ] && [ "$CURRENT_BUILD" != "unknown" ]; then
    echo -e "${YELLOW}Current build time: ${CURRENT_BUILD}${NC}"
else
    echo -e "${YELLOW}No current build time found (first deployment)${NC}"
fi

# Generate new build time
NEW_BUILD_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo -e "${GREEN}Building new app image with build time: ${NEW_BUILD_TIME}${NC}"

# Build new demo app image with build time
docker build --build-arg BUILD_TIME="$NEW_BUILD_TIME" -t "$DEMO_IMAGE" -f "$PROJECT_ROOT/demo/Dockerfile" "$PROJECT_ROOT/demo"

# Load image into kind
echo -e "${GREEN}Loading new image into kind cluster...${NC}"
kind load docker-image "$DEMO_IMAGE" --name "$CLUSTER_NAME"

# Get current StatefulSet template hash (if any)
echo -e "\n${BLUE}Current StatefulSet state:${NC}"
kubectl get sts "$STS_NAME" -n "$NAMESPACE" -o jsonpath='{.metadata.annotations.rollout-operator/state}' 2>/dev/null || echo "none"
kubectl get sts "$STS_NAME" -n "$NAMESPACE" -o jsonpath='{.metadata.annotations.rollout-operator/last-template-hash}' 2>/dev/null && echo ""

# Update StatefulSet with new image and build time
echo -e "\n${GREEN}Updating StatefulSet with new image...${NC}"
kubectl patch statefulset "$STS_NAME" -n "$NAMESPACE" -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"env\":[{\"name\":\"BUILD_TIME\",\"value\":\"${NEW_BUILD_TIME}\"}]}]}}}}"

# Wait a moment for operator to detect the change
sleep 3

# Check operator logs
echo -e "\n${YELLOW}=== Operator Logs (last 20 lines) ===${NC}"
kubectl logs deployment/rollout-operator -n "$NAMESPACE" --tail=20 | grep -E "(demo-sts|New template|Rollout|planned|rolling|hash|ERROR)" || kubectl logs deployment/rollout-operator -n "$NAMESPACE" --tail=20

# Check StatefulSet annotations
echo -e "\n${BLUE}=== StatefulSet Annotations ===${NC}"
kubectl get sts "$STS_NAME" -n "$NAMESPACE" -o jsonpath='{.metadata.annotations}' | python3 -m json.tool 2>/dev/null | grep -E "(rollout-operator|state|hash)" || kubectl get sts "$STS_NAME" -n "$NAMESPACE" -o yaml | grep -A 10 "annotations:" | grep -E "(rollout-operator|state|hash)"

# Show pod status
echo -e "\n${BLUE}=== Pod Status ===${NC}"
kubectl get pods -n "$NAMESPACE" -l app=demo-sts -o wide | head -10

# Check if any pods have the new build time
echo -e "\n${BLUE}Checking pod build times...${NC}"
kubectl get pods -n "$NAMESPACE" -l app=demo-sts -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].env[?(@.name=="BUILD_TIME")].value}{"\n"}{end}' 2>/dev/null | head -5

echo -e "\n${GREEN}=== Test Complete ===${NC}"
echo -e "${BLUE}New build time: ${NEW_BUILD_TIME}${NC}"
echo -e "${BLUE}Watch operator logs: ${GREEN}kubectl logs -f deployment/rollout-operator -n ${NAMESPACE}${NC}"
echo -e "${BLUE}Watch pods: ${GREEN}kubectl get pods -n ${NAMESPACE} -w${NC}"
echo -e "${BLUE}Check pod logs: ${GREEN}kubectl logs <pod-name> -n ${NAMESPACE} | grep BUILD${NC}"

