from __future__ import annotations

import unittest

from openubmc_target_runtime import (
    framed_telnet_command,
    parse_telnet_command_output,
    telnet_command_markers,
)


class TelnetPrimitiveContractTests(unittest.TestCase):
    def test_public_frame_round_trip_preserves_output_and_return_code(self) -> None:
        token = "ab" * 16
        start, return_code, end = telnet_command_markers(token)
        command = framed_telnet_command("printf hello", frame_token=token)
        raw = (
            b"shell echo\r\n"
            + start
            + b"\x1b[31mhello\x1b[0m"
            + b"\n"
            + return_code
            + b"7\n"
            + end
        )

        result = parse_telnet_command_output(
            raw,
            frame_token=token,
            last_read_status="matched",
        )

        self.assertIn("__OPENUBMC_" + token + "_START__", command)
        self.assertTrue(result.framing_complete)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "hello")
        self.assertFalse(result.timed_out)


if __name__ == "__main__":
    unittest.main()
