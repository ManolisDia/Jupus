from eval.error_classes import ERROR_CLASSES, get_active_error_classes


def test_all_seed_classes_have_id_name_description():
    for cls in ERROR_CLASSES:
        assert cls.get("id")
        assert cls.get("name")
        assert cls.get("description")


def test_ids_are_unique():
    ids = [cls["id"] for cls in ERROR_CLASSES]
    assert len(ids) == len(set(ids))


def test_get_active_error_classes_returns_all_seed_classes():
    active = get_active_error_classes()
    assert {c["id"] for c in active} == {c["id"] for c in ERROR_CLASSES}
