"""Packaging smoke tests — the tree must always be importable."""


def test_package_importable():
    import invest

    assert invest.__version__


def test_dependencies_importable():
    """Fail loudly if the pinned stack drifts from what the engine assumes."""

    import duckdb  # noqa: F401
    import openpyxl  # noqa: F401
    import pandas  # noqa: F401
