from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reconstructing_raster_icons.safe_svg import SecurityViolation, validate_svg


class SafeSvgTests(unittest.TestCase):
    def write_svg(self, payload: str | bytes, *, suffix: str = ".svg") -> Path:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        path = Path(temporary_directory.name) / f"candidate{suffix}"
        path.write_bytes(payload.encode("utf-8") if isinstance(payload, str) else payload)
        return path

    def assert_rejected(self, payload: str | bytes) -> None:
        with self.assertRaises(SecurityViolation):
            validate_svg(self.write_svg(payload))

    def test_accepts_the_documented_safe_subset_and_one_leading_declaration(self) -> None:
        path = self.write_svg(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
            'role="img" aria-labelledby="icon-title">'
            '<title id="icon-title">Safe icon</title>'
            '<g fill="currentColor" stroke="none">'
            '<path id="body" fill-rule="evenodd" d="M 8 8 H 56 V 56 H 8 Z"/>'
            '<circle id="dot" cx="32" cy="32" r="4"/>'
            '</g></svg>'
        )

        document = validate_svg(path)

        self.assertEqual(document.source, path)
        self.assertEqual(document.element_count, 5)
        self.assertIn(b"currentColor", document.xml_bytes)

    def test_rejects_entity_before_xml_parse(self) -> None:
        path = self.write_svg('<!DOCTYPE svg [<!ENTITY x "boom">]><svg>&x;</svg>')
        with patch(
            "reconstructing_raster_icons.safe_svg.DefusedElementTree.fromstring",
            side_effect=AssertionError("XML parser must not run"),
        ):
            with self.assertRaises(SecurityViolation):
                validate_svg(path)

    def test_rejects_whitespace_obfuscated_dtd_before_xml_parse(self) -> None:
        payloads = (
            "<! DoCtYpE svg><svg/>",
            '<!\nEnTiTy x "boom"><svg/>',
            "<! D O C T Y P E svg><svg/>",
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                path = self.write_svg(payload)
                with patch(
                    "reconstructing_raster_icons.safe_svg.DefusedElementTree.fromstring",
                    side_effect=AssertionError("XML parser must not run"),
                ):
                    with self.assertRaises(SecurityViolation):
                        validate_svg(path)

    def test_rejects_noncanonical_declaration_before_xml_parse(self) -> None:
        path = self.write_svg('<?XML version="1.0"?><svg/>')
        with patch(
            "reconstructing_raster_icons.safe_svg.DefusedElementTree.fromstring",
            side_effect=AssertionError("XML parser must not run"),
        ):
            with self.assertRaises(SecurityViolation):
                validate_svg(path)

    def test_rejects_external_image_and_event_handler(self) -> None:
        self.assert_rejected(
            '<svg xmlns="http://www.w3.org/2000/svg" onload="x()">'
            '<image href="https://example.test/x"/></svg>'
        )

    def test_rejects_forbidden_scheme_after_character_reference_decode(self) -> None:
        self.assert_rejected(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<title>java&#x73;cript:alert</title><rect width="1" height="1"/>'
            "</svg>"
        )

    def test_rejects_all_forbidden_raw_constructs(self) -> None:
        payloads = {
            "doctype": "<!DOCTYPE svg><svg/>",
            "entity": '<!ENTITY x "boom"><svg/>',
            "processing instruction": "<?xml version=\"1.0\"?><?target value?><svg/>",
            "second declaration": "<?xml version=\"1.0\"?><?xml version=\"1.0\"?><svg/>",
            "nul": b'<svg xmlns="http://www.w3.org/2000/svg">\x00</svg>',
            "data URI": '<svg xmlns="http://www.w3.org/2000/svg"><path id="data:x" d="M0 0"/></svg>',
            "external URI": '<svg xmlns="http://www.w3.org/2000/svg"><desc>https://example.test</desc></svg>',
            "non UTF-8": b'<svg xmlns="http://www.w3.org/2000/svg">\xff</svg>',
            "declared non UTF-8": '<?xml version="1.0" encoding="ISO-8859-1"?><svg/>',
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                self.assert_rejected(payload)

    def test_rejects_forbidden_elements_attributes_and_values(self) -> None:
        payloads = {
            "use": '<svg xmlns="http://www.w3.org/2000/svg"><use href="#x"/></svg>',
            "style": '<svg xmlns="http://www.w3.org/2000/svg"><style>path{fill:red}</style></svg>',
            "text": '<svg xmlns="http://www.w3.org/2000/svg"><text>unsafe</text></svg>',
            "transform": '<svg xmlns="http://www.w3.org/2000/svg"><path transform="scale(2)" d="M0 0"/></svg>',
            "event handler": '<svg xmlns="http://www.w3.org/2000/svg"><path onload="x()" d="M0 0"/></svg>',
            "CSS URL": '<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(#paint)" d="M0 0"/></svg>',
            "foreign namespace": '<svg xmlns="http://www.w3.org/2000/svg"><x:path xmlns:x="urn:evil" d="M0 0"/></svg>',
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                self.assert_rejected(payload)

    def test_rejects_malformed_or_nonfinite_path_data(self) -> None:
        path_values = {
            "non-finite exponent": "M1e999 0 L 1 1",
            "invalid arc flag": "M0 0 A 1 1 0 2 0 2 2",
            "stray exponent marker": "M0 0 E 1 1",
            "move only": "M0 0",
            "zero-length line": "M0 0 L0 0",
        }
        for name, path_data in path_values.items():
            with self.subTest(name=name):
                self.assert_rejected(
                    f'<svg xmlns="http://www.w3.org/2000/svg"><path d="{path_data}"/></svg>'
                )

    def test_accepts_finite_path_exponent_syntax(self) -> None:
        path = self.write_svg(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<path d="M1e2 1e-2 L2E2 -3e+1"/></svg>'
        )
        document = validate_svg(path)
        self.assertEqual(document.path_data_characters, len("M1e2 1e-2 L2E2 -3e+1"))

    def test_rejects_more_than_ten_thousand_elements(self) -> None:
        payload = '<svg xmlns="http://www.w3.org/2000/svg">' + ("<g/>" * 10_000) + "</svg>"
        self.assert_rejected(payload)

    def test_rejects_more_than_two_million_path_characters(self) -> None:
        payload = (
            '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0 '
            + ("0 " * 1_000_000)
            + '00"/></svg>'
        )
        self.assert_rejected(payload)

    def test_rejects_nesting_deeper_than_sixty_four_elements(self) -> None:
        payload = '<svg xmlns="http://www.w3.org/2000/svg">' + ("<g>" * 64) + ("</g>" * 64) + "</svg>"
        self.assert_rejected(payload)

    def test_rejects_input_larger_than_five_mib(self) -> None:
        payload = b'<svg xmlns="http://www.w3.org/2000/svg"><desc>'
        payload += b"x" * (5 * 1024 * 1024)
        payload += b"</desc></svg>"
        self.assert_rejected(payload)

    def test_reads_replaced_regular_path_from_the_single_open_descriptor(self) -> None:
        original = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        replacement = '<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'
        path = self.write_svg(original)
        real_open = os.open
        opened = 0

        def replace_after_open(source: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
            nonlocal opened
            descriptor = real_open(source, flags, mode)
            opened += 1
            path.unlink()
            path.write_text(replacement, encoding="utf-8")
            return descriptor

        with patch.object(os, "open", side_effect=replace_after_open):
            document = validate_svg(path)

        self.assertEqual(opened, 1)
        self.assertEqual(document.xml_bytes, original.encode("utf-8"))
        self.assertEqual(path.read_text(encoding="utf-8"), replacement)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixture requires POSIX")
    def test_rejects_symlink_and_fifo_without_blocking(self) -> None:
        regular = self.write_svg('<svg xmlns="http://www.w3.org/2000/svg"/>')
        symlink = regular.with_name("linked.svg")
        fifo = regular.with_name("candidate.fifo")
        symlink.symlink_to(regular)
        os.mkfifo(fifo)
        with self.assertRaises(SecurityViolation):
            validate_svg(symlink)
        with self.assertRaises(SecurityViolation):
            validate_svg(fifo)

    def test_fails_closed_without_safe_no_follow_or_nonblocking_open(self) -> None:
        path = self.write_svg('<svg xmlns="http://www.w3.org/2000/svg"/>')
        for flag_name in ("O_NOFOLLOW", "O_NONBLOCK"):
            with self.subTest(flag=flag_name), patch.object(os, flag_name, 0):
                with self.assertRaises(SecurityViolation):
                    validate_svg(path)


if __name__ == "__main__":
    unittest.main()
