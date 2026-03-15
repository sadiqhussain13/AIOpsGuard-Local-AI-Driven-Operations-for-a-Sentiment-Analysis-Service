IMAGE_TAG ?= latest
DOCKER_REPO ?= aiopsguard
APP_IMAGE := $(DOCKER_REPO)/sentiment-app:$(IMAGE_TAG)
ANOMALY_IMAGE := $(DOCKER_REPO)/anomaly-detector:$(IMAGE_TAG)

.PHONY: build push deploy monitor test clean train

## Build all Docker images
build:
	docker build -t $(APP_IMAGE) app/
	docker build -t $(ANOMALY_IMAGE) anomaly_detector/

## Push Docker images to registry
push:
	docker push $(APP_IMAGE)
	docker push $(ANOMALY_IMAGE)

## Deploy to Minikube via Ansible
deploy:
	ansible-playbook ansible/deploy.yml

## Apply K8s manifests directly
apply:
	kubectl apply -k k8s/overlays/dev

## Train the anomaly detection model
train:
	python anomaly_detector/train_anomaly_model.py \
		--data data/logs.csv \
		--output model/anomaly_model.pkl

## Start the full local stack with docker-compose
up:
	docker compose up --build -d

## Stop the local docker-compose stack
down:
	docker compose down

## Open Grafana, Prometheus, MLflow in browser
monitor:
	@echo "Grafana:    http://localhost:3000  (admin/admin)"
	@echo "Prometheus: http://localhost:9090"
	@echo "MLflow:     http://localhost:5001"
	@xdg-open http://localhost:3000 2>/dev/null || open http://localhost:3000 2>/dev/null || true

## Run unit tests
test:
	pytest tests/ -v --tb=short

## Run the Locust load test (headless, 20 users, 60 seconds)
load-test:
	locust -f load_test/locustfile.py \
		--host http://localhost:5000 \
		--headless -u 20 -r 5 -t 60s

## Run the AIOps agent once
agent:
	python agent/agent.py

## Clean up generated artefacts
clean:
	rm -rf model/ __pycache__ .pytest_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
