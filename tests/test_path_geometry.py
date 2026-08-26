from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconstructing_raster_icons.geometry import (  # noqa: E402
    HAUSDORFF_DISTANCE_EVALUATION_BUDGET,
    PathIntegrityError,
    PolylineSubpath,
    _global_incidence_signature,
    _hausdorff_operation_estimate,
    _intersection_signature,
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

    def test_flattened_curve_has_no_consecutive_duplicate_vertices(self) -> None:
        subpath = flatten_svg_path("M0 0 C1 1 0 -1 -3 0", delta=6.4)[0]

        self.assertTrue(all(first != second for first, second in zip(subpath.points, subpath.points[1:])))

    def test_closed_flattened_path_has_one_non_degenerate_closure_segment(self) -> None:
        subpath = flatten_svg_path("M0 0 L2 0 L2 2 L0 0 Z", delta=1.0)[0]

        self.assertTrue(subpath.closed)
        self.assertEqual(subpath.points.count((0.0, 0.0)), 2)
        self.assertTrue(all(first != second for first, second in zip(subpath.points, subpath.points[1:])))

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
    @staticmethod
    def _three_path_junction(
        scale: float, epsilon_ratio: float = 0.01
    ) -> tuple[PolylineSubpath, ...]:
        epsilon = epsilon_ratio * scale
        center = (0.137 * scale, -0.223 * scale)

        def shifted(point: tuple[float, float]) -> tuple[float, float]:
            return center[0] + point[0], center[1] + point[1]

        return (
            PolylineSubpath(
                (shifted((-scale, epsilon)), center, shifted((scale, -2.0 * epsilon))),
                False,
            ),
            PolylineSubpath(
                (shifted((epsilon, -scale)), center, shifted((-2.0 * epsilon, scale))),
                False,
            ),
            PolylineSubpath(
                (
                    shifted((-scale, scale + epsilon)),
                    center,
                    shifted((scale, -scale + 2.0 * epsilon)),
                ),
                False,
            ),
        )

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

    def test_symmetric_hausdorff_is_scale_robust_at_one_nanometre(self) -> None:
        source = PolylineSubpath(points=((0.0, 0.0), (1e-9, 0.0)), closed=False)
        targets = (
            PolylineSubpath(points=((0.0, -1e-15), (0.0, 1e-15)), closed=False),
            PolylineSubpath(points=((1e-9, -1e-15), (1e-9, 1e-15)), closed=False),
        )

        self.assertAlmostEqual(symmetric_hausdorff((source,), targets), 5e-10, delta=2e-13)

    def test_hausdorff_operation_estimate_counts_roots_and_target_scans(self) -> None:
        self.assertEqual(_hausdorff_operation_estimate(2, 3), 396)

    def test_symmetric_hausdorff_fails_closed_before_pathological_distance_work(self) -> None:
        source = PolylineSubpath(((0.0, 0.0), (1.0, 0.0)), False)
        targets = tuple(
            PolylineSubpath(((float(index), -1.0), (float(index), 1.0)), False)
            for index in range(110)
        )

        self.assertGreater(_hausdorff_operation_estimate(1, 110), HAUSDORFF_DISTANCE_EVALUATION_BUDGET)
        with self.assertRaisesRegex(PathIntegrityError, "Hausdorff distance-evaluation budget"):
            symmetric_hausdorff((source,), targets)

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

    def test_simplification_preserves_intersection_multiplicity_and_order(self) -> None:
        zigzag = PolylineSubpath(
            points=((-3.0, -0.1), (-1.0, 0.1), (1.0, -0.1), (3.0, 0.1)),
            closed=False,
        )
        bar = PolylineSubpath(points=((-3.0, 0.0), (3.0, 0.0)), closed=False)

        self.assertEqual(simplify_subpaths((zigzag, bar), delta=0.3), (zigzag, bar))

    def test_simplification_preserves_self_intersection_multiplicity(self) -> None:
        self_crossing = PolylineSubpath(
            points=(
                (-3.0, -0.1),
                (-1.0, 0.1),
                (1.0, -0.1),
                (3.0, 0.1),
                (3.0, 0.0),
                (-3.0, 0.0),
            ),
            closed=False,
        )

        self.assertEqual(simplify_subpaths((self_crossing,), delta=0.3), (self_crossing,))

    def test_topology_signature_classifies_scaled_overlap_and_endpoint(self) -> None:
        horizontal = PolylineSubpath(((0.0, 0.0), (1e-9, 0.0)), False)
        overlap = PolylineSubpath(((0.5e-9, 0.0), (1.5e-9, 0.0)), False)
        endpoint = PolylineSubpath(((1e-9, 0.0), (1e-9, 1e-9)), False)

        self.assertEqual(
            _intersection_signature((horizontal, overlap)),
            ((0, 1, (("overlap", 0, 1, 0, 1),)),),
        )
        self.assertEqual(
            _intersection_signature((horizontal, endpoint)),
            ((0, 1, (("endpoint", 0, 0, 0, 0),)),),
        )
        self.assertEqual(_global_incidence_signature((horizontal, overlap))[0][1][0][2], "overlap")
        self.assertEqual(_global_incidence_signature((horizontal, endpoint))[0][1][0][2], "endpoint")

    def test_topology_signature_distinguishes_vertex_touch_from_crossing(self) -> None:
        horizontal = PolylineSubpath(((-1.0, 0.0), (1.0, 0.0)), False)
        touching = PolylineSubpath(((-1.0, 1.0), (0.0, 0.0), (1.0, 1.0)), False)
        crossing = PolylineSubpath(((-1.0, 1.0), (0.0, 0.0), (1.0, -1.0)), False)

        self.assertEqual(_intersection_signature((horizontal, touching))[0][2][0][0], "touch")
        self.assertEqual(_intersection_signature((horizontal, crossing))[0][2][0][0], "transverse")
        self.assertEqual(_global_incidence_signature((horizontal, touching))[0][1][0][2], "touch")
        self.assertEqual(_global_incidence_signature((horizontal, crossing))[0][1][0][2], "transverse")

    def test_global_junction_incidence_survives_simplification_at_all_scales(self) -> None:
        for scale in (1e-9, 1.0, 1e9):
            with self.subTest(scale=scale):
                junction = self._three_path_junction(scale)
                separated = tuple(
                    PolylineSubpath((path.points[0], path.points[-1]), False) for path in junction
                )

                self.assertEqual(_intersection_signature(junction), _intersection_signature(separated))
                self.assertNotEqual(
                    _global_incidence_signature(junction),
                    _global_incidence_signature(separated),
                )
                self.assertEqual(simplify_subpaths(junction, delta=0.05 * scale), junction)

    def test_global_incidence_does_not_merge_close_distinct_crossings(self) -> None:
        for scale in (1e-9, 1.0, 1e9):
            with self.subTest(scale=scale):
                junction = self._three_path_junction(scale, epsilon_ratio=1e-12)
                separated = tuple(
                    PolylineSubpath((path.points[0], path.points[-1]), False) for path in junction
                )

                self.assertEqual(len(_global_incidence_signature(junction)), 1)
                self.assertEqual(len(_global_incidence_signature(separated)), 3)

    def test_valid_simplification_can_retain_a_shared_junction(self) -> None:
        shared = (
            PolylineSubpath(((-1.0, 0.0), (0.0, 0.0), (1.0, 0.0)), False),
            PolylineSubpath(((0.0, -1.0), (0.0, 0.0), (0.0, 1.0)), False),
            PolylineSubpath(((-1.0, -1.0), (0.0, 0.0), (1.0, 1.0)), False),
        )

        simplified = simplify_subpaths(shared, delta=0.2)

        self.assertTrue(all(len(path.points) == 2 for path in simplified))
        self.assertEqual(_global_incidence_signature(shared), _global_incidence_signature(simplified))

    def test_global_incidence_preserves_cyclic_branch_arrangement(self) -> None:
        horizontal = PolylineSubpath(((-1.0, 0.0), (1.0, 0.0)), False)
        vertical = PolylineSubpath(((0.0, -1.0), (0.0, 1.0)), False)
        rising = PolylineSubpath(((-1.0, -1.0), (1.0, 1.0)), False)
        falling = PolylineSubpath(((-1.0, 1.0), (1.0, -1.0)), False)

        self.assertEqual(
            _intersection_signature((horizontal, vertical, rising)),
            _intersection_signature((horizontal, vertical, falling)),
        )
        self.assertNotEqual(
            _global_incidence_signature((horizontal, vertical, rising)),
            _global_incidence_signature((horizontal, vertical, falling)),
        )

    def test_open_loop_cannot_collapse_to_a_degenerate_candidate(self) -> None:
        source = PolylineSubpath(points=((0.0, 0.0), (1.0, 0.0), (0.0, 0.0)), closed=False)

        self.assertEqual(simplify_subpaths((source,), delta=3.0), (source,))


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

    def test_intentional_intersection_does_not_hide_minimum_gap_failure(self) -> None:
        components = {
            "a": ((-1.0, -1.0), (1.0, 1.0)),
            "b": ((-1.0, 1.0), (1.0, -1.0)),
        }
        constraints = {
            "intentional_intersections": [{"first": "a", "second": "b"}],
            "minimum_intentional_gaps": [{"first": "a", "second": "b", "minimum_gap": 1.0}],
        }

        result = evaluate_geometry_constraints(components, constraints, delta=1.0)
        gap = next(item for item in result.measurements if item.constraint_kind == "gap")

        self.assertFalse(result.passed)
        self.assertFalse(gap.passed)
        self.assertEqual(gap.details["observed_gap"], 0.0)

    def test_tolerance_equality_uses_scale_aware_machine_epsilon(self) -> None:
        constraint = {
            "radial": [
                {"component_id": "circle", "geometry": "circle", "center": (0.0, 0.0), "radius": 10.0, "tolerance": 1.0}
            ]
        }

        equal = evaluate_geometry_constraints(
            {"circle": ((11.0, 0.0), (0.0, 11.0))}, constraint, delta=1.0
        )
        over = evaluate_geometry_constraints(
            {"circle": ((11.000000001, 0.0), (0.0, 11.000000001))}, constraint, delta=1.0
        )

        self.assertTrue(equal.passed)
        self.assertFalse(over.passed)

    def test_radial_evaluator_rejects_mixed_or_incomplete_variants(self) -> None:
        component = {"circle": ((1.0, 0.0), (0.0, 1.0))}
        mixed = {
            "radial": [
                {
                    "component_id": "circle",
                    "geometry": "circle",
                    "center": (0.0, 0.0),
                    "radius": 1.0,
                    "radius_x": 1.0,
                    "radius_y": 1.0,
                    "tolerance": 0.0,
                }
            ]
        }
        incomplete = {
            "radial": [
                {"component_id": "circle", "geometry": "ellipse", "center": (0.0, 0.0), "radius_x": 1.0, "tolerance": 0.0}
            ]
        }

        for constraints in (mixed, incomplete):
            with self.subTest(constraints=constraints), self.assertRaisesRegex(ValueError, "radial.*variant"):
                evaluate_geometry_constraints(component, constraints, delta=1.0)


if __name__ == "__main__":
    unittest.main()
