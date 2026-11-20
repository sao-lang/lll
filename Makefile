# AgentKit 开发入口：每个模块一条命令独立闭环（CL-4）
PYTHON ?= .venv/Scripts/python
PYTEST := $(PYTHON) -m pytest

.PHONY: install test demo demo-r1 demo-r2 demo-r3 demo-r4 demo-r5 demo-r6 demo-r7 gate gate-r1 gate-r2 gate-r3 gate-r4 gate-r5 gate-r6 gate-r7 gate-r12

install:
	$(PYTHON) -m pip install -e ".[dev]"

demo: demo-r1 demo-r2 demo-r3 demo-r4 demo-r5 demo-r6 demo-r7

demo-r1:
	$(PYTHON) tests/demos/demo_r1.py

demo-r2:
	$(PYTHON) tests/demos/demo_r2.py

demo-r3:
	$(PYTHON) tests/demos/demo_r3.py

demo-r4:
	$(PYTHON) tests/demos/demo_r4.py

demo-r5:
	$(PYTHON) tests/demos/demo_r5.py

demo-r6:
	$(PYTHON) tests/demos/demo_r6.py

demo-r7:
	$(PYTHON) tests/demos/demo_r7.py

test:
	$(PYTEST) tests/r1 tests/r2 tests/r3 tests/r4 tests/r5 tests/r6 tests/r7 tests/ports -q

gate: gate-r1 gate-r2 gate-r3 gate-r4 gate-r5 gate-r6 gate-r7 gate-r12

gate-r1:
	$(PYTHON) tests/tools/gate_check.py --module r1

gate-r2:
	$(PYTHON) tests/tools/gate_check.py --module r2

gate-r3:
	$(PYTHON) tests/tools/gate_check.py --module r3

gate-r4:
	$(PYTHON) tests/tools/gate_check.py --module r4

gate-r5:
	$(PYTHON) tests/tools/gate_check.py --module r5

gate-r6:
	$(PYTHON) tests/tools/gate_check.py --module r6

gate-r7:
	$(PYTHON) tests/tools/gate_check.py --module r7

gate-r12:
	$(PYTHON) tests/tools/gate_check.py --module r12
