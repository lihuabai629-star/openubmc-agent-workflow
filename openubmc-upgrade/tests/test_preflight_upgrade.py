from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preflight_upgrade.py"
SPEC = importlib.util.spec_from_file_location("preflight_upgrade", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class UpgradePreflightTests(unittest.TestCase):
    def test_advertised_methods_preserve_upgrade_preference(self) -> None:
        self.assertEqual(
            MODULE.advertised_methods(
                {
                    "HttpPushUri": "/push",
                    "MultipartHttpPushUri": "/multipart",
                    "Actions": {
                        "#UpdateService.SimpleUpdate": {"target": "/simple"}
                    },
                }
            ),
            ["MultipartHttpPushUri", "HttpPushUri", "SimpleUpdate"],
        )

    def test_missing_or_malformed_methods_are_not_advertised(self) -> None:
        self.assertEqual(MODULE.advertised_methods({}), [])
        self.assertEqual(
            MODULE.advertised_methods(
                {
                    "MultipartHttpPushUri": "",
                    "HttpPushUri": 7,
                    "Actions": {"#UpdateService.SimpleUpdate": {}},
                }
            ),
            [],
        )

    def test_advertised_uris_keep_only_target_supplied_paths(self) -> None:
        self.assertEqual(
            MODULE.advertised_uris(
                {
                    "HttpPushUri": "/redfish/v1/UpdateService/upload",
                    "MultipartHttpPushUri": "",
                    "Actions": {
                        "#UpdateService.SimpleUpdate": {"target": "/simple"}
                    },
                }
            ),
            {
                "HttpPushUri": "/redfish/v1/UpdateService/upload",
                "SimpleUpdate": "/simple",
            },
        )

    def test_max_image_size_requires_a_positive_integer(self) -> None:
        self.assertEqual(
            MODULE.advertised_max_image_size({"MaxImageSizeBytes": 1024}),
            1024,
        )
        for value in (None, 0, -1, True, "1024"):
            self.assertIsNone(
                MODULE.advertised_max_image_size({"MaxImageSizeBytes": value})
            )

    def test_legacy_http_push_collection_endpoint_is_reported(self) -> None:
        self.assertEqual(
            MODULE.compatibility_warnings(
                {
                    "HttpPushUri": (
                        "/redfish/v1/UpdateService/FirmwareInventory"
                    )
                }
            ),
            ["legacy-http-push-collection-endpoint"],
        )
        self.assertEqual(
            MODULE.compatibility_warnings(
                {
                    "MultipartHttpPushUri": "/multipart",
                    "HttpPushUri": (
                        "/redfish/v1/UpdateService/FirmwareInventory"
                    ),
                }
            ),
            [],
        )

    def test_upload_plan_selects_legacy_multipart_staging(self) -> None:
        self.assertEqual(
            MODULE.upload_plan(
                {
                    "HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory",
                    "Actions": {
                        "#UpdateService.SimpleUpdate": {"target": "/simple"}
                    },
                }
            ),
            {
                "method": "HttpPushUri",
                "encoding": "multipart/form-data",
                "compatibility_mode": "legacy-http-push-multipart",
                "staged_activation_required": True,
            },
        )

    def test_upload_plan_keeps_standard_http_push_binary(self) -> None:
        self.assertEqual(
            MODULE.upload_plan({"HttpPushUri": "/redfish/v1/UpdateService/upload"}),
            {
                "method": "HttpPushUri",
                "encoding": "application/octet-stream",
                "compatibility_mode": "",
                "staged_activation_required": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
