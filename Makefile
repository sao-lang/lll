# AgentKit 开发入口：每个模块一条命令独立闭环（CL-4）
PYTHON ?= .venv/Scripts/python
PYTEST := $(PYTHON) -m pytest

.PHONY: install test demo demo-m1 demo-m2 demo-m3 demo-m4 gate gate-m1 gate-m2 gate-m3 gate-m4

install:
	$(PYTHON) -m pip install -e ".[dev]"

demo: demo-m1 demo-m2 demo-m3 demo-m4

demo-m1:
	$(PYTHON) tests/demos/demo_m1.py

demo-m2:
	$(PYTHON) tests/demos/demo_m2.py

demo-m3:
	$(PYTHON) tests/demos/demo_m3.py

demo-m4:
	$(PYTHON) tests/demos/demo_m4.py

test:
	$(PYTEST) tests -q

gate: gate-m1 gate-m2 gate-m3 gate-m4

gate-m1:
	$(PYTHON) tests/tools/gate_check.py --module m1

gate-m2:
	$(PYTHON) tests/tools/gate_check.py --module m2

gate-m3:
	$(PYTHON) tests/tools/gate_check.py --module m3

gate-m4:
	$(PYTHON) tests/tools/gate_check.py --module m4
