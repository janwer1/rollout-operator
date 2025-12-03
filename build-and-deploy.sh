#!/bin/bash

sudo podman build -t ttl.sh/rollout-operator:24h .
sudo podman push ttl.sh/rollout-operator:24h
kubectl apply -f operator.yml


cd demo
sudo podman build -f Dockerfile -t ttl.sh/demo-app:24h .
sudo podman push ttl.sh/demo-app:24h
kubectl apply -f sts.yml
