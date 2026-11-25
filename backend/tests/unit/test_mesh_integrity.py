"""Mesh integrity analysis.

The point of these tests is the distinction that makes the feature usable: a
file that cannot be parsed must never look like a file with a problem. If that
line blurs, every STEP file in a library becomes a false positive and the badge
stops meaning anything.
"""

import numpy as np
import pytest
import trimesh

from backend.app.services.mesh_integrity import (
    STATUS_OK,
    STATUS_PROBLEMS,
    STATUS_TOO_LARGE,
    STATUS_UNREADABLE,
    STATUS_UNSUPPORTED,
    analyze_file,
    is_supported,
)


def _write(mesh, path):
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    return path


@pytest.fixture
def good_cube(tmp_path):
    return _write(trimesh.creation.box(extents=(20, 20, 20)), tmp_path / "cube.stl")


@pytest.fixture
def holed_cube(tmp_path):
    """A cube with two triangles removed, leaving one square hole."""
    box = trimesh.creation.box(extents=(20, 20, 20))
    open_mesh = trimesh.Trimesh(vertices=box.vertices, faces=box.faces[:-2], process=False)
    return _write(open_mesh, tmp_path / "holed.stl")


def test_good_mesh_is_not_flagged(good_cube):
    result = analyze_file(good_cube)
    assert result["status"] == STATUS_OK
    assert result["findings"] == []
    assert result["stats"]["watertight"] is True
    assert result["stats"]["holes"] == 0


def test_hole_is_detected(holed_cube):
    result = analyze_file(holed_cube)
    assert result["status"] == STATUS_PROBLEMS
    codes = {f["code"] for f in result["findings"]}
    assert "holes" in codes
    assert result["stats"]["holes"] == 1
    assert result["stats"]["boundary_edges"] == 4
    assert result["stats"]["watertight"] is False


def test_non_manifold_edge_is_detected(tmp_path):
    """A third face hanging off an existing edge: valid geometry, unprintable."""
    box = trimesh.creation.box(extents=(20, 20, 20))
    a, b = box.faces[0][0], box.faces[0][1]
    vertices = np.vstack([box.vertices, [[50.0, 50.0, 50.0]]])
    faces = np.vstack([box.faces, [[a, b, len(box.vertices)]]])
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    result = analyze_file(_write(mesh, tmp_path / "nonmanifold.stl"))
    assert result["status"] == STATUS_PROBLEMS
    assert "non_manifold_edges" in {f["code"] for f in result["findings"]}


def test_step_file_is_unsupported_not_broken(tmp_path):
    """The false-positive case this feature would otherwise create."""
    step = tmp_path / "bracket.step"
    step.write_text(
        "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('bracket'),'2;1');\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    )
    result = analyze_file(step)
    assert result["status"] == STATUS_UNSUPPORTED
    assert result["status"] != STATUS_PROBLEMS
    assert result["findings"] == []


def test_unparseable_stl_is_unreadable_not_problems(tmp_path):
    junk = tmp_path / "corrupt.stl"
    junk.write_bytes(b"not an stl at all, just bytes" * 40)
    result = analyze_file(junk)
    assert result["status"] == STATUS_UNREADABLE
    assert result["findings"] == []


def test_file_too_small_to_hold_a_triangle(tmp_path):
    stub = tmp_path / "stub.stl"
    stub.write_bytes(b"\x00" * 32)
    assert analyze_file(stub)["status"] == STATUS_UNREADABLE


def test_size_ceiling_reports_too_large_not_a_problem(good_cube):
    result = analyze_file(good_cube, max_file_bytes=10)
    assert result["status"] == STATUS_TOO_LARGE
    assert result["findings"] == []


def test_face_ceiling_reports_too_large(good_cube):
    result = analyze_file(good_cube, max_faces=2)
    assert result["status"] == STATUS_TOO_LARGE
    assert result["findings"] == []


def test_sliced_gcode_3mf_is_not_treated_as_geometry():
    assert is_supported("part.3mf") is True
    assert is_supported("part.gcode.3mf") is False
    assert is_supported("part.step") is False
    assert is_supported("part.STL") is True


def test_degenerate_faces_are_recorded_but_never_badged(tmp_path):
    """Recorded in stats, absent from findings: common and not worth a badge."""
    box = trimesh.creation.box(extents=(20, 20, 20))
    a, b = box.faces[0][0], box.faces[0][1]
    faces = np.vstack([box.faces, [[a, b, a]]])
    mesh = trimesh.Trimesh(vertices=box.vertices, faces=faces, process=False)
    result = analyze_file(_write(mesh, tmp_path / "degenerate.stl"))
    assert "degenerate_faces" in result["stats"]
    assert "degenerate_faces" not in {f["code"] for f in result["findings"]}
