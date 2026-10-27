from pathlib import Path

from sales0.config import Settings
from sales0.scraper.browser import profile_dir, run_headless


def test_profiles_live_under_the_data_dir_split_by_mode(tmp_path: Path) -> None:
    live = Settings(linkedin_mode="live", data_dir=str(tmp_path))
    fixture = Settings(linkedin_mode="fixture", data_dir=str(tmp_path))
    assert profile_dir(live) == tmp_path / "linkedin-profile"
    assert profile_dir(fixture) == tmp_path / "linkedin-profile-fixture"


def test_fixture_mode_is_always_headless_and_live_follows_the_setting() -> None:
    assert run_headless(Settings(linkedin_mode="fixture", linkedin_headless=False)) is True
    assert run_headless(Settings(linkedin_mode="live", linkedin_headless=False)) is False
    assert run_headless(Settings(linkedin_mode="live", linkedin_headless=True)) is True


def test_default_profile_dir_is_inside_the_gitignored_repo_data_dir() -> None:
    settings = Settings(linkedin_mode="live", data_dir="data")
    assert profile_dir(settings).parts[-2:] == ("data", "linkedin-profile")
