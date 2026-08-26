from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconstructing_raster_icons.geometry import (  # noqa: E402
    PathIntegrityError,
    PolylineSubpath,
    evaluate_geometry_constraints,
    flatten_svg_path,
    simplify_subpaths,
    symmetric_hausdorff,
)


class PathFlatteningTests(unittest.TestCase):
    def test_quadratic_uses_midpoint_de_casteljau_subdivision(self) -> None:
        subpath = flatten_svg_path("M 0 0 Q 2 2 4 0", delta=4.0)[0]

        self.assertEqual(subpath.points, ((0.0, 0.0), (2.0, 1.0), (4.0, 0.0)))
        self.assertFalse(subpath.closed)

    def test_degenerate_chord_uses_control_hull_diameter(self) -> None:
        subpath = flatten_svg_path("M 0 0 C 2 0 -2 0 0 0", delta=1.0)[0]

        self.assertGreater(len(subpath.points), 2)
        self.assertEqual(subpath.points[0], subpath.points[-1])
        self.assertGreater(max(abs(x) for x, _ in subpath.points), 0.5)

    def test_subdivision_fails_closed_at_depth_32(self) -> None:
        with self.assertRaisesRegex(PathIntegrityError, "depth 32"):
            flatten_svg_path("M 0 0 Q 1 1 2 0", delta=1e-100)

    def test_zero_length_segment_fails_path_integrity(self) -> None:
        with self.assertRaisesRegex(PathIntegrityError, "zero-length"):
            flatten_svg_path("M 0 0 L 0 0", delta=1.0)

    def test_svg2_arc_conversion_handles_radius_correction_and_quadrants(self) -> None:
        subpath = flatten_svg_path("M 0 0 A 1 1 0 0 1 4 0", delta=0.05)[0]

        self.assertEqual(subpath.points[0], (0.0, 0.0))
        self.assertAlmostEqual(subpath.points[-1][0], 4.0, places=12)
        self.assertAlmostEqual(subpath.points[-1][1], 0.0, places=12)
        self.assertLess(min(y for _, y in subpath.points), -1.9)
        self.assertTrue(any(abs(x - 2.0) < 1e-12 and y < -1.9 for x, y in subpath.points))

    def test_arc_flags_are_not_delegated_to_library_sampling_defaults(self) -> None:
        small = flatten_svg_path("M 1 0 A 1 1 0 0 1 0 1", delta=0.05)[0]
        large = flatten_svg_path("M 1 0 A 1 1 0 1 1 0 1", delta=0.05)[0]

        self.assertLess(len(small.points), len(large.points))
        self.assertTrue(all(x >= -1e-12 and y >= -1e-12 for x, y in small.points))
        self.assertTrue(any(x > 1.5 or y > 1.5 for x, y in large.points))


class SimplificationTests(unittest.TestCase):
    def test_closed_paths_rotate_to_stable_lexicographic_start(self) -> None:
        source = PolylineSubpath(
            points=((3.0, 3.0), (1.0, 3.0), (1.0, 1.0), (3.0, 1.0), (3.0, 3.0)),
            closed=True,
        )

        simplified = simplify_subpaths((source,), delta=0.2)[0]

        self.assertEqual(simplified.points[0], (1.0, 1.0))
        self.assertEqual(simplified.points[-1], (1.0, 1.0))
        self.assertGreater(simplified.signed_area, 0.0)

    def test_simplification_preserves_hole_winding_and_containment(self) -> None:
        outer = PolylineSubpath(
            points=((0.0, 0.0), (6.0, 0.0), (6.0, 6.0), (0.0, 6.0), (0.0, 0.0)),
            closed=True,
        )
        hole = PolylineSubpath(
            points=((2.0, 2.0), (2.0, 4.0), (4.0, 4.0), (4.0, 2.0), (2.0, 2.0)),
            closed=True,
        )

        simplified = simplify_subpaths((outer, hole), delta=0.5)

        self.assertGreater(simplified[0].signed_area, 0.0)
        self.assertLess(simplified[1].signed_area, 0.0)
        self.assertEqual(tuple(path.closed for path in simplified), (True, True))

    def test_symmetric_hausdorff_uses_continuous_segments_not_vertices(self) -> None:
        horizontal = PolylineSubpath(points=((0.0, 0.0), (4.0, 0.0)), closed=False)
        vertical = PolylineSubpath(points=((2.0, -1.0), (2.0, 1.0)), closed=False)

        distance = symmetric_hausdorff((horizontal,), (vertical,))

        self.assertAlmostEqual(distance, 2.0, places=12)
        vertex_only = math.sqrt(5.0)
        self.assertLess(distance, vertex_only)

    def test_simplification_is_bounded_by_continuous_symmetric_hausdorff(self) -> None:
        original = PolylineSubpath(
            points=((0.0, 0.0), (1.0, 0.1), (2.0, 0.0), (3.0, -0.1), (4.0, 0.0)),
            closed=False,
        )

        simplified = simplify_subpaths((original,), delta=0.5)

        self.assertLessEqual(symmetric_hausdorff((original,), simplified), 0.25 + 1e-12)
        self.assertEqual(simplified[0].points[0], original.points[0])
        self.assertEqual(simplified[0].points[-1], original.points[-1])

    def test_simplification_does_not_create_a_new_subpath_intersection(self) -> None:
        bent = PolylineSubpath(points=((0.0, 0.0), (1.0, 1.0), (2.0, 0.0)), closed=False)
        short_crossbar = PolylineSubpath(points=((1.0, -0.2), (1.0, 0.2)), closed=False)

        simplified = simplify_subpaths((bent, short_crossbar), delta=2.2)

        self.assertEqual(simplified[0].points, bent.points)


class GeometryConstraintTests(unittest.TestCase):
    def test_universal_constraint_catalog_reports_measurement_and_tolerance(self) -> None:
        components = {
            "horizontal": {"points": ((0.0, 0.0), (10.0, 0.0)), "stroke_width": 2.0, "cap": "round", "join": "round"},
            "vertical": {"points": ((5.0, -5.0), (5.0, 5.0))},
            "circle": {"points": ((7.0, 5.0), (5.0, 7.0), (3.0, 5.0), (5.0, 3.0), (7.0, 5.0))},
        }
        constraints = {
            "lines": [{"component_id": "horizontal", "start": (0.0, 0.0), "end": (10.0, 0.0), "tolerance": 0.01}],
            "orthogonality": [{"first": "horizontal", "second": "vertical", "tolerance": 0.01}],
            "parallelism": [{"first": "horizontal", "second": "horizontal", "tolerance": 0.01}],
            "endpoints": [{"component_id": "horizontal", "start": (0.0, 0.0), "end": (10.0, 0.0), "tolerance": 0.01}],
            "radial": [{"component_id": "circle", "geometry": "circle", "center": (5.0, 5.0), "radius": 2.0, "tolerance": 0.01}],
            "symmetry": [{"component_id": "circle", "axis_start": (5.0, 0.0), "axis_end": (5.0, 10.0), "tolerance": 0.01}],
            "strokes": [{"component_id": "horizontal", "expected_width": 2.0, "cap": "round", "join": "round", "tolerance": 0.01}],
            "intentional_intersections": [
                {"first": "horizontal", "second": "vertical"},
                {"first": "vertical", "second": "circle"},
            ],
            "minimum_intentional_gaps": [{"first": "horizontal", "second": "circle", "minimum_gap": 0.0}],
        }

        result = evaluate_geometry_constraints(components, constraints, delta=1.0)

        self.assertTrue(result.passed)
        self.assertEqual(
            {measurement.constraint_kind for measurement in result.measurements},
            {"line", "orthogonality", "parallelism", "endpoints", "radial", "symmetry", "stroke", "intersection", "gap"},
        )
        self.assertTrue(all(math.isfinite(measurement.measured_deviation) for measurement in result.measurements))
        self.assertTrue(all(math.isfinite(measurement.tolerance) for measurement in result.measurements))

    def test_constraints_are_data_driven_not_component_name_driven(self) -> None:
        components = {"anything": {"points": ((0.0, 0.0), (3.0, 0.25))}}
        constraints = {
            "lines": [{"component_id": "anything", "start": (0.0, 0.0), "end": (3.0, 0.0), "tolerance": 0.1}]
        }

        result = evaluate_geometry_constraints(components, constraints, delta=1.0)

        self.assertFalse(result.passed)
        self.assertGreater(result.measurements[0].measured_deviation, result.measurements[0].tolerance)

    def test_normalized_map_points_are_scaled_by_canonical_canvas(self) -> None:
        components = {"line": {"points": ((0.0, 0.0), (64.0, 16.0))}}
        constraints = {
            "endpoints": [
                {"component_id": "line", "start": (0.0, 0.0), "end": (1.0, 0.5), "tolerance": 0.01}
            ]
        }

        result = evaluate_geometry_constraints(
            components,
            constraints,
            delta=1.0,
            canonical_canvas=(64.0, 32.0),
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.measurements[0].measured_deviation, 0.0)


if __name__ == "__main__":
    unittest.main()
