PY        ?= python3.11
VENV      := .venv
SC        := $(VENV)/bin/safechange
PROFILE   ?= safechange
CTX       ?= $(PROFILE)
MINIKUBE_FLAGS ?= --cpus=2 --memory=2g

.PHONY: help venv cluster kubeconfigs demo demo-fake test clean lint

help:
	@echo "make venv         - create .venv and install safechange[dev]"
	@echo "make cluster      - start minikube, namespaces, RBAC, sample app, control ConfigMap, kubeconfigs"
	@echo "make demo         - scripted end-to-end demo against minikube"
	@echo "make demo-fake    - same demo against the in-memory fake backend (no cluster)"
	@echo "make test         - run the pytest suite (fake backend)"
	@echo "make clean        - minikube delete -p $(PROFILE) + remove local state"

$(SC): pyproject.toml
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -e '.[dev]'
	@touch $(SC)

venv: $(SC)

cluster: venv
	minikube start -p $(PROFILE) $(MINIKUBE_FLAGS)
	kubectl --context $(CTX) apply -f deploy/namespaces.yaml
	kubectl --context $(CTX) apply -f deploy/rbac.yaml
	kubectl --context $(CTX) apply -f deploy/control-configmap.yaml
	kubectl --context $(CTX) apply -f deploy/web.yaml
	kubectl --context $(CTX) -n demo rollout status deploy/web --timeout=180s
	ADMIN_CTX=$(CTX) ./scripts/make-kubeconfigs.sh

kubeconfigs:
	ADMIN_CTX=$(CTX) ./scripts/make-kubeconfigs.sh

demo: venv
	CTX=$(CTX) ./scripts/demo.sh

demo-fake: venv
	BACKEND=fake ./scripts/demo.sh

test: venv
	$(VENV)/bin/pytest

clean:
	-minikube delete -p $(PROFILE)
	rm -rf .safechange .kube-safechange .demo-state
