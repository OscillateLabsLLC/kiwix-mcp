default:
    just --list

install:
    pip install -e ".[dev]"

test:
    pytest

lint:
    ruff check .

fmt:
    ruff format .

build:
    python -m build

clean:
    rm -rf dist/ build/ *.egg-info .pytest_cache .coverage
