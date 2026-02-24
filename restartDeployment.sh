#!/bin/bash
kubectl rollout restart deployment openshrimp-agent -n openshrimp
kubectl rollout restart deployment openshrimp-frontend -n openshrimp
