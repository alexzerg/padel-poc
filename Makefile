IMAGE ?= ghcr.io/your-user/padel-finder
TAG   ?= 0.1.0
# Override when deploying to a non-amd64 cluster.
PLATFORM ?= linux/amd64
RELEASE ?= padel-finder
NAMESPACE ?= padel-finder
KUBE_CONTEXT ?=

KUBECTL = kubectl $(if $(KUBE_CONTEXT),--context $(KUBE_CONTEXT),)
HELM = helm $(if $(KUBE_CONTEXT),--kube-context $(KUBE_CONTEXT),)

.PHONY: build run stop lint template push deploy status uninstall

build:
	docker build --platform $(PLATFORM) -t padel:$(TAG) .

run: build
	docker rm -f padel-local 2>/dev/null || true
	docker run --rm -d --name padel-local -p 18080:8080 padel:$(TAG)
	@echo "UI: http://127.0.0.1:18080/"

stop:
	docker rm -f padel-local 2>/dev/null || true

lint:
	$(HELM) lint helm/padel

template:
	$(HELM) template $(RELEASE) helm/padel --namespace $(NAMESPACE)

push: build
	@test "$(IMAGE)" != "ghcr.io/your-user/padel-finder" || (echo "Set IMAGE to your container registry path"; exit 1)
	docker tag padel:$(TAG) $(IMAGE):$(TAG)
	docker push $(IMAGE):$(TAG)

deploy:
	$(KUBECTL) create namespace $(NAMESPACE) --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(HELM) upgrade --install $(RELEASE) helm/padel --namespace $(NAMESPACE) \
		--set image.repository=$(IMAGE) --set image.tag=$(TAG) --wait --timeout 5m

status:
	$(KUBECTL) --namespace $(NAMESPACE) get deploy,po,svc,ing

uninstall:
	$(HELM) uninstall $(RELEASE) --namespace $(NAMESPACE)
