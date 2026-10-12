from __future__ import annotations

import pytest

from pikpak_fastlink.cli import parse_args


def test_root_literal_maps_to_empty_parent_id(tmp_path):
    _args, config = parse_args(
        [
            "import-json",
            "--file",
            str(tmp_path / "input.json"),
            "--target-parent-id",
            "root",
            "--state-file",
            str(tmp_path / "state.sqlite3"),
        ]
    )
    assert config.target_parent_id == ""


SELECTOR_COMMANDS = {
    "import-json": (
        ["--file", "in.json", "--state-file", "state.sqlite3"],
        "--target-parent-id", "--target-path", "target_path",
    ),
    "batch-import-json": (
        ["--input-dir", "in", "--state-dir", "state"],
        "--target-parent-id", "--target-path", "target_path",
    ),
    "batch-check-json": (
        ["--input-dir", "in", "--state-dir", "state", "--output-dir", "out"],
        "--target-parent-id", "--target-path", "target_path",
    ),
    "export-json": (
        ["--output-file", "out.json", "--state-file", "state.json"],
        "--source-parent-id", "--source-path", "source_path",
    ),
    "compare-folder": (
        ["--local-folder", "."],
        "--remote-folder-id", "--remote-path", "remote_path",
    ),
}


@pytest.mark.parametrize("command", SELECTOR_COMMANDS)
@pytest.mark.parametrize("raw, expected", [
    ("/", "/"),
    ("/Media", "/Media"),
    ("/Media/\u52d5\u753b Shows /", "/Media/\u52d5\u753b Shows "),
])
def test_remote_path_selects_folder_by_exact_path(command, raw, expected):
    options, _id_flag, path_flag, field = SELECTOR_COMMANDS[command]
    _args, config = parse_args([command, *options, path_flag, raw])
    assert getattr(config, field) == expected


@pytest.mark.parametrize("command", SELECTOR_COMMANDS)
@pytest.mark.parametrize("selectors", [[], ["id", "path"]])
def test_remote_folder_requires_exactly_one_id_or_path(command, selectors):
    options, id_flag, path_flag, _field = SELECTOR_COMMANDS[command]
    values = {"id": [id_flag, "root"], "path": [path_flag, "/Media"]}
    with pytest.raises(SystemExit):
        parse_args([command, *options, *(arg for s in selectors for arg in values[s])])


@pytest.mark.parametrize("path", [
    "", "   ", "Media", "./Media", "\\Media", "/Media\\Shows", "//",
    "/Media//Shows", "/Media//", "/./Media", "/Media/..", "/Media/.",
])
def test_invalid_remote_path_is_rejected_before_any_request(path):
    with pytest.raises(ValueError, match="remote path"):
        parse_args(["import-json", "--file", "in.json", "--state-file", "s.sqlite3",
                    "--target-path", path])


def test_invalid_worker_count_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="workers"):
        parse_args(
            [
                "import-json",
                "--file",
                str(tmp_path / "input.json"),
                "--target-parent-id",
                "root",
                "--state-file",
                str(tmp_path / "state.sqlite3"),
                "--workers",
                "0",
            ]
        )


def test_input_cannot_collide_with_retry_output(tmp_path):
    with pytest.raises(ValueError, match="state artifacts"):
        parse_args(
            [
                "import-json",
                "--file",
                str(tmp_path / "job.retry.export.json"),
                "--target-parent-id",
                "root",
                "--state-file",
                str(tmp_path / "job.sqlite3"),
            ]
        )


def test_generate_json_configures_local_calculator(tmp_path):
    source = tmp_path / "source"
    _args, config = parse_args(
        [
            "generate-json",
            "--source-dir",
            str(source),
            "--output-file",
            str(tmp_path / "out.json"),
            "--workers",
            "3",
        ]
    )
    assert config.command == "generate_json"
    assert config.source_dir == source.resolve()
    assert config.workers == 3


def test_compare_folder_directions_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        parse_args(
            [
                "compare-folder",
                "--local-folder",
                str(tmp_path),
                "--remote-folder-id",
                "root",
                "--local-only",
                "--remote-only",
            ]
        )


@pytest.mark.parametrize(
    "options, error",
    [
        ([], SystemExit),
        (["--local-folder", ".", "--local-json", "local.json"], SystemExit),
        (["--local-json", "local.json"], ValueError),
        (["--local-json", "", "--output-file", "out.json"], ValueError),
        (["--local-folder", ".", "--output-file", "out.json"], ValueError),
        (["--local-json", "local.json", "--output-file", "local.json"], ValueError),
        (
            [
                "--local-json",
                "local.json",
                "--output-file",
                "out.json",
                "--local-only",
                "--remote-only",
            ],
            SystemExit,
        ),
    ],
)
def test_compare_json_argument_contract(options, error):
    with pytest.raises(error):
        parse_args(["compare-folder", "--remote-folder-id", "root", *options])


def test_compare_json_rejects_output_alias(tmp_path):
    source = tmp_path / "local.json"
    source.write_text('{"files": []}', encoding="utf-8")
    alias = tmp_path / "alias.json"
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match="distinct"):
        parse_args(
            [
                "compare-folder",
                "--local-json",
                str(source),
                "--remote-folder-id",
                "root",
                "--output-file",
                str(alias),
            ]
        )
