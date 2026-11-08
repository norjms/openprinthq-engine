"""Filament list from a plain G-code's slicer config block."""

from pathlib import Path

from backend.app.services.gcode_filaments import filaments_from_config, parse_config_lines, parse_gcode_filaments

FIXTURES = Path(__file__).resolve().parents[2] / "_fixtures" / "material_units"


def test_reads_types_colours_and_usage_from_the_config_block():
    out = parse_gcode_filaments(FIXTURES / "orca_multicolour_tail.gcode")
    assert out == [
        {"tool": 0, "type": "PLA", "color": "#F0F0F0", "used": True},
        {"tool": 1, "type": "PETG", "color": "#1B1B1B", "used": True},
        {"tool": 2, "type": "PLA", "color": "#FF0000", "used": False},
    ]


def test_comma_separated_lists_and_missing_colours():
    config = parse_config_lines("; filament_type = PLA,ABS\n; filament used [mm] = 10.0,0\n")
    assert filaments_from_config(config) == [
        {"tool": 0, "type": "PLA", "color": None, "used": True},
        {"tool": 1, "type": "ABS", "color": None, "used": False},
    ]


def test_no_declared_filaments_returns_empty(tmp_path):
    f = tmp_path / "plain.gcode"
    f.write_text("G28\nG1 X0\n")
    assert parse_gcode_filaments(f) == []
    assert parse_gcode_filaments(tmp_path / "missing.gcode") == []


def test_large_file_reads_head_and_tail_only(tmp_path):
    f = tmp_path / "big.gcode"
    with f.open("w") as fh:
        fh.write("; filament_type = PLA;PLA\n")
        fh.write("G1 X1 Y1 E0.1\n" * 120_000)  # ~1.7 MB of moves
        fh.write("; filament_colour = #000000;#FFFFFF\n")
    out = parse_gcode_filaments(f)
    assert [x["color"] for x in out] == ["#000000", "#FFFFFF"]
