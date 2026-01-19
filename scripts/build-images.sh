#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

OPERATOR_IMAGE="janwer/rollout-operator:24h"
DEMO_IMAGE="janwer/demo-app:24h"

echo -e "${GREEN}=== Building Images ===${NC}"

# Build operator image
echo -e "${GREEN}Building operator image: ${OPERATOR_IMAGE}${NC}"
docker build -t "$OPERATOR_IMAGE" "$PROJECT_ROOT"

# Build demo app image
echo -e "${GREEN}Building demo app image: ${DEMO_IMAGE}${NC}"
docker build -f "$PROJECT_ROOT/demo/Dockerfile" -t "$DEMO_IMAGE" "$PROJECT_ROOT/demo/"

# Push images to Docker Hub (only if PUSH_IMAGES is set)
if [ "${PUSH_IMAGES:-false}" = "true" ]; then
    echo -e "${GREEN}Pushing images to Docker Hub...${NC}"
    docker push "$OPERATOR_IMAGE"
    docker push "$DEMO_IMAGE"
    echo -e "${GREEN}=== Images Built and Pushed ===${NC}"
else
    echo -e "${YELLOW}Skipping push (set PUSH_IMAGES=true to push to Docker Hub)${NC}"
    echo -e "${GREEN}=== Images Built (not pushed) ===${NC}"
fi

echo -e "${GREEN}Operator: ${OPERATOR_IMAGE}${NC}"
echo -e "${GREEN}Demo App: ${DEMO_IMAGE}${NC}"

# Export for use in other scripts
export OPERATOR_IMAGE
export DEMO_IMAGE

