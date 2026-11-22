from __future__ import annotations

import pytest
from pydantic import ValidationError

from enikk_core_domain.repositories import SensitivePaths


def test_directories_cover_everything_below_and_files_match_exactly() -> None:
    paths = SensitivePaths(paths=("docs/adr/", "AGENTS.md"))
    assert paths.matches("docs/adr/0001-x.md")
    assert paths.matches("AGENTS.md")
    assert not paths.matches("docs/adrx.md")
    assert not paths.matches("src/AGENTS.md")
    assert paths.among(["README.md", "docs/adr/a.md"]) == ("docs/adr/a.md",)


def test_globs_are_rejected_so_the_list_means_what_it_says() -> None:
    with pytest.raises(ValidationError):
        SensitivePaths(paths=("docs/*",))
