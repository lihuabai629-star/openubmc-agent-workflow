from __future__ import annotations

from pathlib import Path
import sys
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import is_read_only_mdb_query  # noqa: E402


class MdbQueryGrammarTests(unittest.TestCase):
    def test_block_io_read_is_the_only_admitted_call(self) -> None:
        self.assertTrue(
            is_read_only_mdb_query(
                [
                    "call",
                    "Eeprom_NIC_010107",
                    "bmc.kepler.Chip.BlockIO",
                    "Read",
                    "0",
                    "0",
                    "16",
                ]
            )
        )
        for command in (
            ["call", "Eeprom0", "bmc.kepler.Chip.BlockIO", "Write", "0", "0", "16"],
            ["call", "Eeprom0", "bmc.kepler.Chip.Control", "Read", "0", "0", "16"],
            ["call", "Eeprom0", "bmc.kepler.Chip.BlockIO", "Read", "-1", "0", "16"],
            ["call", "Eeprom0", "bmc.kepler.Chip.BlockIO", "Read", "0", "0", "4097"],
            ["call", "Eeprom0", "bmc.kepler.Chip.BlockIO", "Read", "9" * 4097, "0", "16"],
            ["call", "Eeprom0", "bmc.kepler.Chip.BlockIO", "Read", "١", "0", "16"],
            ["call", "Eeprom0;reboot", "bmc.kepler.Chip.BlockIO", "Read", "0", "0", "16"],
            ["call", "E" * 257, "bmc.kepler.Chip.BlockIO", "Read", "0", "0", "16"],
        ):
            with self.subTest(command=command):
                self.assertFalse(is_read_only_mdb_query(command))

    def test_block_io_offsets_are_bounded_uint32_values(self) -> None:
        maximum = str((1 << 32) - 1)
        self.assertTrue(
            is_read_only_mdb_query(
                [
                    "call",
                    "Eeprom0",
                    "bmc.kepler.Chip.BlockIO",
                    "Read",
                    maximum,
                    maximum,
                    "4096",
                ]
            )
        )
        self.assertFalse(
            is_read_only_mdb_query(
                [
                    "call",
                    "Eeprom0",
                    "bmc.kepler.Chip.BlockIO",
                    "Read",
                    str(1 << 32),
                    "0",
                    "1",
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
