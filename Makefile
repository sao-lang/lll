# AgentKit 开发入口：每个模块一条命令独立闭环（CL-4）
PYTHON ?= .venv/Scripts/python
PYTEST := $(PYTHON) -m pytest

.PHONY: install test demo demo-r1 demo-r2 gate gate-r1 gate-r2 contract-check contract-update

install:
	$(PYTHON) -m pip install -e ".[dev]"

demo: demo-r1 demo-r2

demo-r1:
	$(PYTHON) tests/demos/demo_r1.py

demo-r2:
	$(PYTHON) tests/demos/demo_r2.py

test:
	$(PYTEST) tests/r1 tests/r2 -q

gate: gate-r1 gate-r2

gate-r1:
	$(PYTHON) tests/tools/gate_check.py --module r1

gate-r2:
	$(PYTHON) tests/tools/gate_check.py --module r2

# G5 设计稳定性门禁：契约锁检查 / 评审后更新基线
contract-check:
	$(PYTHON) tests/tools/contract_lock.py check

contract-update:
	$(PYTHON) tests/tools/contract_lock.py update
