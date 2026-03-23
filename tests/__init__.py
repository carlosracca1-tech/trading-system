# tests/__init__.py
# Test suite for Trading System
#
# Test categories:
#   unit/        — pure Python, no DB, no network
#   integration/ — requires DB (uses SQLite in-memory by default)
#   e2e/         — requires full Docker environment
#
# Run all tests:   make test
# Run unit only:   make test-unit
# Run with cov:    make test-cov
