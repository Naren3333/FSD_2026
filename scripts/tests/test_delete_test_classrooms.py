"""Which classroom names scripts/delete_test_classrooms.py deletes: only the exact names test runs gave them."""

import pytest
from delete_test_classrooms import matches


@pytest.mark.parametrize(
    "name",
    ["Browser 1760000000000", "Chat 1760000000000", "Mocked chat 1760000000000", "Generation 1760000000000",
     "Integration 0a1b2c3d"],
)  # fmt: skip
def test_names_test_runs_gave_are_deleted(name):
    assert matches(name, include_eval=False)


@pytest.mark.parametrize(
    "name",
    ["Browser 123", "Browser 17600000000001", " Browser 1760000000000", "Browser 1760000000000 (copy)",
     "Integration 0A1B2C3D", "Integration 0a1b2c3g", "Integration 0a1b2c3", "Chemistry 1760000000000",
     "Golden eval C1"],
)  # fmt: skip
def test_other_names_are_kept(name):
    assert not matches(name, include_eval=False)


def test_the_eval_classrooms_only_with_eval():
    assert matches("Golden eval C1", include_eval=True) and matches("Golden eval C2", include_eval=True)
    assert not matches("Golden eval C3", include_eval=True)
