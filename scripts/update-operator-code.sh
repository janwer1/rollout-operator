#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

NAMESPACE="demo-sts"
CONFIGMAP_NAME="rollout-operator-code"

echo -e "${GREEN}=== Updating Operator Code from ConfigMap ===${NC}"

# Create or update ConfigMap with the operator code
echo -e "${YELLOW}Creating/updating ConfigMap with operator code...${NC}"
kubectl create configmap "$CONFIGMAP_NAME" \
    --from-file=rollout_operator.py="$PROJECT_ROOT/rollout_operator.py" \
    -n "$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f -

# Restart the operator to pick up the new code
echo -e "${YELLOW}Restarting operator to load new code...${NC}"
kubectl rollout restart deployment/rollout-operator -n "$NAMESPACE"

echo -e "${GREEN}=== Code Updated ===${NC}"
echo -e "${GREEN}Watch logs: kubectl logs -f deployment/rollout-operator -n ${NAMESPACE}${NC}"

