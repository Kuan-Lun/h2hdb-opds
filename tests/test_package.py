from importlib import import_module


def test_package_is_importable() -> None:
    import_module("h2hdb_opds")
