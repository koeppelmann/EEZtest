# EEZtest — convenience targets.  Requires a config.yaml (copy config.example.yaml).
PY ?= python3
CONFIG ?= config.yaml

.PHONY: install check run report clean

install:
	$(PY) -m pip install -r requirements.txt

check:
	$(PY) -m eeztest check --config $(CONFIG)

run:
	$(PY) -m eeztest run --config $(CONFIG)

# Short smoke run (2 minutes) for a quick health signal.
smoke:
	$(PY) -m eeztest run --config $(CONFIG) --duration 120

report:
	$(PY) -m eeztest report --config $(CONFIG)

clean:
	rm -rf __pycache__ eeztest/__pycache__ eeztest/workers/__pycache__ .solcx
