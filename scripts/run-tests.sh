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

OPERATOR_IMAGE="janwer/rollout-operator:24h"
DEMO_IMAGE="janwer/demo-app:24h"
CLUSTER_NAME="rollout-operator-test"

echo -e "${GREEN}=== Rollout Operator Test Suite ===${NC}"
echo -e "${BLUE}Operator Image: ${OPERATOR_IMAGE}${NC}"
echo -e "${BLUE}Demo Image: ${DEMO_IMAGE}${NC}"
echo ""

# Step 1: Build images (don't push, we'll load into kind)
echo -e "${GREEN}[1/5] Building images (will load into kind, not pushing)...${NC}"
PUSH_IMAGES=false "$SCRIPT_DIR/build-images.sh"
echo ""

# Step 2: Use manifests directly (images are already set)
echo -e "${GREEN}[2/5] Using manifests with configured images...${NC}"
cp "$PROJECT_ROOT/operator.yml" /tmp/operator-test.yml
cp "$PROJECT_ROOT/demo/sts.yml" /tmp/demo-sts-test.yml
echo -e "${GREEN}Manifests ready${NC}"
echo ""

# Step 3: Setup kind cluster
echo -e "${GREEN}[3/5] Setting up kind cluster...${NC}"
# Create cluster if it doesn't exist
if ! kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
    echo -e "${YELLOW}Creating kind cluster...${NC}"
    cat <<EOF | kind create cluster --name "$CLUSTER_NAME" --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
EOF
    # Wait for cluster to be ready
    echo -e "${YELLOW}Waiting for cluster to be ready...${NC}"
    kubectl wait --for=condition=Ready nodes --all --timeout=120s
else
    echo -e "${YELLOW}Cluster ${CLUSTER_NAME} already exists, reusing...${NC}"
fi
echo ""

# Step 4: Load images into kind (if using docker) or they'll be pulled
echo -e "${GREEN}[4/5] Loading images into kind cluster...${NC}"
# Try to load from docker, but images can also be pulled from registry
if docker image inspect "$OPERATOR_IMAGE" &>/dev/null; then
    kind load docker-image "$OPERATOR_IMAGE" --name "$CLUSTER_NAME"
fi
if docker image inspect "$DEMO_IMAGE" &>/dev/null; then
    kind load docker-image "$DEMO_IMAGE" --name "$CLUSTER_NAME"
fi
echo ""

# Step 5: Deploy operator and demo
echo -e "${GREEN}[5/5] Deploying operator and demo StatefulSet...${NC}"
# Delete namespace if it exists (clean slate)
if kubectl get namespace demo-sts &>/dev/null; then
    echo -e "${YELLOW}Deleting existing namespace demo-sts...${NC}"
    kubectl delete namespace demo-sts --wait=true --timeout=60s || true
fi

kubectl apply -f /tmp/operator-test.yml

# Wait for operator to be ready
echo -e "${YELLOW}Waiting for operator to be ready...${NC}"
kubectl wait --for=condition=available deployment/rollout-operator -n demo-sts --timeout=120s || {
    echo -e "${RED}Operator deployment failed. Checking logs...${NC}"
    kubectl logs -l app=rollout-operator -n demo-sts --tail=50
    exit 1
}

# Deploy demo StatefulSet
kubectl apply -f /tmp/demo-sts-test.yml

# Wait for StatefulSet pods to be ready (with a reasonable timeout for 32 pods)
echo -e "${YELLOW}Waiting for StatefulSet pods to be ready (this may take a while for 32 pods)...${NC}"
kubectl wait --for=condition=ready pod -l app=demo-sts -n demo-sts --timeout=600s || {
    echo -e "${YELLOW}Not all pods are ready yet. Checking status...${NC}"
    kubectl get pods -n demo-sts
}

echo ""
echo -e "${GREEN}=== Setup Complete ===${NC}"
echo -e "${GREEN}Cluster: ${CLUSTER_NAME}${NC}"
echo ""
echo -e "${BLUE}Useful commands:${NC}"
echo -e "  Operator logs:     ${GREEN}kubectl logs -f deployment/rollout-operator -n demo-sts${NC}"
echo -e "  StatefulSet:      ${GREEN}kubectl get sts demo-sts -n demo-sts${NC}"
echo -e "  Pods:             ${GREEN}kubectl get pods -n demo-sts${NC}"
echo -e "  Test rollout:     ${GREEN}$SCRIPT_DIR/test-rollout.sh${NC}"
echo ""
echo -e "${BLUE}To trigger a rollout, update the StatefulSet:${NC}"
echo -e "  ${GREEN}kubectl patch sts demo-sts -n demo-sts -p '{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"test\":\"'\$(date +%s)'\"}}}}}'${NC}"
echo ""

# Cleanup temp files
rm -f /tmp/operator-test.yml /tmp/demo-sts-test.yml

