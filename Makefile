# AgentKit 开发入口：每个模块一条命令独立闭环（CL-4）
PYTHON ?= .venv/Scripts/python
PYTEST := $(PYTHON) -m pytest

.PHONY: install test demo demo-r1 demo-r2 demo-r3 demo-r4 demo-r5 demo-r6 demo-r7 demo-r8 demo-r9 demo-r10 demo-r11 demo-r12 gate gate-r1 gate-r2 gate-r3 gate-r4 gate-r5 gate-r6 gate-r7 gate-r8 gate-r9 gate-r10 gate-r11 gate-r12

install:
	$(PYTHON) -m pip install -e ".[dev]"

demo: demo-r1 demo-r2 demo-r3 demo-r4 demo-r5 demo-r6 demo-r7 demo-r8 demo-r9 demo-r10 demo-r11 demo-r12

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

demo-r8:
	$(PYTHON) tests/demos/demo_r8.py

demo-r9:
	$(PYTHON) tests/demos/demo_r9.py

demo-r10:
	$(PYTHON) tests/demos/demo_r10.py

demo-r11:
	$(PYTHON) tests/demos/demo_r11.py

demo-r12:
	$(PYTHON) tests/demos/demo_r12.py

test:
	$(PYTEST) tests/r1 tests/r2 tests/r3 tests/r4 tests/r5 tests/r6 tests/r7 tests/r8 tests/r9 tests/r10 tests/r11 tests/r12 tests/ports -q

gate: gate-r1 gate-r2 gate-r3 gate-r4 gate-r5 gate-r6 gate-r7 gate-r8 gate-r9 gate-r10 gate-r11 gate-r12

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

gate-r8:
	$(PYTHON) tests/tools/gate_check.py --module r8

gate-r9:
	$(PYTHON) tests/tools/gate_check.py --module r9

gate-r10:
	$(PYTHON) tests/tools/gate_check.py --module r10

gate-r11:
	$(PYTHON) tests/tools/gate_check.py --module r11

gate-r12:
	$(PYTHON) tests/tools/gate_check.py --module r12

# 源码纯净门禁：调用 conductor 器官脚本（唯一真相源），基线锁 2026-11-21 HEAD 存量。
purity:
	$(PYTHON) "C:/Users/30895/.workbuddy/skills/conductor/organs/conductor-review/scripts/source_purity.py" check --baseline tests/tools/baselines/purity-HEAD-20261121.json --strict
