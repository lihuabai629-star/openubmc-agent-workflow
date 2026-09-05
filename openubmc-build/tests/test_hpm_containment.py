from __future__ import annotations

import gzip
import builtins
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import random
import struct
import tarfile
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_hpm_containment.py"
_SPEC = importlib.util.spec_from_file_location("hpm_containment_under_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
verifier = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verifier)
TEST_KEY = b"0123456789abcdef"


def make_tar(rootfs: bytes, *, members: list[tuple[str, bytes, bytes]] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data, kind in members or [("rootfs_iBMC.img", rootfs, tarfile.REGTYPE)]:
            info = tarfile.TarInfo(name)
            info.size, info.type = len(data), kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "rootfs_iBMC.img"
                info.size = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def make_gpp(rootfs: bytes, *, archive: bytes | None = None) -> bytes:
    if archive is None:
        archive = gzip.compress(make_tar(rootfs), mtime=0)
    body = bytearray(164)
    struct.pack_into("<IIHH", body, 0, 0x55AA55AA, 0, 3, 7)
    for i, (kind, content) in enumerate(((16, b"root-certificate"), (17, b"cms"), (18, b"crl"))):
        struct.pack_into("<III", body, 76 + i * 12, kind, len(body), len(content))
        body.extend(content)
    struct.pack_into("<III", body, 148, 4, len(body), len(archive))
    struct.pack_into("<I", body, 160, 0x33CC33CC)
    body.extend(archive)
    struct.pack_into("<I", body, 4, len(body))
    gpp = bytearray(512)
    struct.pack_into("<I", gpp, 0, 3)
    for i, (kind, content) in enumerate(((0, b"boot-one" * 9), (4, b"boot-two" * 13), (1, body))):
        struct.pack_into("<IIII", gpp, 16 + i * 16, kind, len(gpp), len(content), 0)
        gpp.extend(content)
    return bytes(gpp)


def encrypt_gpp(gpp: bytes, key: bytes = TEST_KEY) -> bytes:
    encrypted = bytearray(b"E" * 256)
    for offset in range(0, len(gpp), 10240):
        chunk = gpp[offset:offset + 10240]
        padding = 16 - len(chunk) % 16
        iv = b"a" + b"\0" * 15
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypted.extend(iv)
        encrypted.extend(encryptor.update(chunk + bytes([padding]) * padding) + encryptor.finalize())
    return bytes(encrypted)


def _action(kind: int, mask: bytes | None = None) -> bytes:
    body = bytes((kind,)) + (mask if mask is not None else b"\x01\0\0\x02")
    return body + bytes(((-sum(body)) % 256,))


def _upload(description: bytes, path: bytes, encrypted: bytes) -> bytes:
    service = bytearray(512)
    struct.pack_into("<II", service, 256, 512, len(encrypted))
    service[264:288] = path.ljust(24, b"\0")
    firmware = b"\x05\x12\0\0\0\0" + description.ljust(21, b"\0")
    mask = b"\x01\0\0\0" if description == b"CONFIG" else b"\0\0\0\x02"
    return _action(2, mask) + firmware + struct.pack("<I", len(service) + len(encrypted)) + service + encrypted


def signed_wrapper(raw: bytes, *, manifest: bytes | None = None) -> bytes:
    if manifest is None:
        manifest = ("Manifest Version: 1.0\nCreate By: Huawei Technology Inc.\n"
                    "Name: rootfs_openUBMC.hpm\nSHA256-Digest: "
                    + hashlib.sha256(raw).hexdigest() + "\n").encode("ascii")
    cms, crl = b"synthetic-cms", b"synthetic-crl"
    header = "".join(f"{v:08x}" for v in (3, 1, len(manifest), 2, len(cms), 3, len(crl))).encode("ascii")
    return header + manifest + cms + crl + raw


def make_hpm(rootfs: bytes, *, key: bytes = TEST_KEY, signed: bool = False,
             archive: bytes | None = None, gpp: bytes | None = None,
             encrypted: bytes | None = None) -> bytes:
    """Synthetic package fixture; CMS/envelope bytes deliberately carry no trust."""
    if encrypted is None:
        encrypted = encrypt_gpp(gpp if gpp is not None else make_gpp(rootfs, archive=archive), key)
    header = bytearray(37)
    header[:8], header[8], header[9] = b"PICMGFWU", 1, 1
    header.append((-sum(header)) % 256)
    raw = (bytes(header) + _action(1)
           + _upload(b"CONFIG", b"/data/conf.tar.gz", encrypt_gpp(b"configuration" * 3, key))
           + _upload(b"APP", b"/data/ipmc.jffs2", encrypted))
    return signed_wrapper(raw) if signed else raw


def app_offset(raw: bytes) -> int:
    config_length = struct.unpack_from("<I", raw, 44 + 33)[0]
    return 44 + 37 + config_length


class HpmContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rootfs = random.Random(42).randbytes(32773)
        self.image = self.root / "rootfs.img"
        self.hpm = self.root / "artifact.hpm"
        self.key = self.root / "private-aes-key"
        self.image.write_bytes(self.rootfs)
        self.key.write_bytes(TEST_KEY)
        self.raw = make_hpm(self.rootfs)
        self.hpm.write_bytes(self.raw)

    def verify(self, raw: bytes | None = None, **kwargs: object) -> dict[str, object]:
        if raw is not None:
            self.hpm.write_bytes(raw)
        return verifier.verify_hpm_containment(self.hpm, self.image,
                                               **{"key_path": self.key, **kwargs})

    def assert_unverified(self, raw: bytes | None = None, reason: str | None = None,
                          **kwargs: object) -> dict[str, object]:
        result = self.verify(raw, **kwargs)
        self.assertEqual(result["status"], "unverified", result)
        self.assertNotIn("payloads", result)
        if reason is not None:
            self.assertEqual(result["reason"], reason, result)
        return result

    def test_raw_multichunk_and_signed_containment(self) -> None:
        for signed in (False, True):
            raw = make_hpm(self.rootfs, signed=signed)
            with self.subTest(signed=signed):
                result = self.verify(raw, expected_artifact_sha256=hashlib.sha256(raw).hexdigest(),
                                     expected_rootfs_sha256=hashlib.sha256(self.rootfs).hexdigest())
                self.assertEqual(result["status"], "verified", result)
                member = result["payloads"]["rootfs_member"]
                self.assertEqual(member["sha256"], hashlib.sha256(self.rootfs).hexdigest())
                self.assertEqual(member["size"], len(self.rootfs))
                self.assertEqual((member["parent"], member["offset"]), ("uncompressed-tar", 512))
                if signed:
                    wrapper = result["payloads"]["signed_wrapper"]
                    self.assertTrue(wrapper["manifest_digest_verified"])
                    self.assertFalse(wrapper["signature_trust_verified"])

    def test_reports_do_not_disclose_paths_key_bytes_or_key_digest(self) -> None:
        for key_bytes in (TEST_KEY, b"wrong-key-value!"):
            self.key.write_bytes(key_bytes)
            result = self.verify()
            rendered = json.dumps(result)
            self.assertNotIn(str(self.root), rendered)
            self.assertNotIn(TEST_KEY.decode(), rendered)
            self.assertNotIn(key_bytes.decode(), rendered)
            self.assertNotIn(hashlib.sha256(TEST_KEY).hexdigest(), rendered)
            self.assertNotIn(hashlib.sha256(key_bytes).hexdigest(), rendered)

    def test_missing_key_and_unsupported_format_fail_closed(self) -> None:
        self.assert_unverified(reason="missing_key", key_path=None)
        self.assert_unverified(b"not-a-supported-hpm", "unsupported_hpm_format", key_path=None)
        self.assert_unverified(key_path=self.root / "missing")

    def test_missing_crypto_backend_never_qualifies_package(self) -> None:
        original = builtins.__import__
        def unavailable(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("cryptography"):
                raise ImportError("synthetic unavailable backend")
            return original(name, *args, **kwargs)
        with mock.patch("builtins.__import__", side_effect=unavailable):
            self.assert_unverified(reason="crypto_backend_unavailable")

    def test_key_length_and_wrong_key_fail_closed(self) -> None:
        for key in (b"", b"a" * 15, b"a" * 17, b"x" * 16):
            self.key.write_bytes(key)
            with self.subTest(length=len(key)):
                self.assert_unverified()

    def test_expected_hashes_and_actual_rootfs_mismatch(self) -> None:
        self.assert_unverified(reason="artifact_hash_mismatch", expected_artifact_sha256="0" * 64)
        self.assert_unverified(reason="rootfs_hash_mismatch", expected_rootfs_sha256="0" * 64)
        self.assert_unverified(reason="invalid_expected_artifact_sha256", expected_artifact_sha256="")
        self.image.write_bytes(b"x" + self.rootfs[1:])
        self.assert_unverified(reason="contained_rootfs_hash_mismatch")
        self.image.write_bytes(self.rootfs + b"x")
        self.assert_unverified(reason="contained_rootfs_size_mismatch")

    def test_size_caps_precede_processing(self) -> None:
        self.assert_unverified(reason="artifact_size_limit", max_artifact_bytes=len(self.raw) - 1)
        self.assert_unverified(reason="rootfs_size_limit", max_rootfs_bytes=len(self.rootfs) - 1)
        for limit in (0, -1, True, "10"):
            self.assert_unverified(reason="invalid_size_limit", max_artifact_bytes=limit)

    def test_nonregular_and_symlink_input_rejected(self) -> None:
        for role in ("hpm", "image", "key"):
            target = getattr(self, role)
            renamed = target.with_suffix(".actual")
            target.rename(renamed)
            target.symlink_to(renamed)
            self.assert_unverified()
            target.unlink()
            renamed.rename(target)
        self.assert_unverified(key_path=self.root)

    def test_path_replacement_and_same_size_write_detected(self) -> None:
        real = verifier._tar_rootfs
        for mutation in ("replace", "write"):
            with self.subTest(mutation=mutation):
                self.hpm.write_bytes(self.raw)
                def drift(*args: object) -> object:
                    output = real(*args)
                    if mutation == "replace":
                        replacement = self.root / "replacement"
                        replacement.write_bytes(self.raw)
                        replacement.replace(self.hpm)
                    else:
                        with self.hpm.open("r+b") as handle:
                            handle.write(b"X")
                    return output
                with mock.patch.object(verifier, "_tar_rootfs", side_effect=drift):
                    self.assert_unverified(reason="artifact_changed_while_reading")

    def test_rootfs_and_key_drift_detected(self) -> None:
        real = verifier._tar_rootfs
        for role, target, content in (("rootfs", self.image, self.rootfs), ("key", self.key, TEST_KEY)):
            with self.subTest(role=role):
                def drift(*args: object) -> object:
                    output = real(*args)
                    target.write_bytes(content)
                    return output
                with mock.patch.object(verifier, "_tar_rootfs", side_effect=drift):
                    self.assert_unverified(reason=role + "_changed_while_reading")

    def test_wrapper_tags_lengths_manifest_and_truncation(self) -> None:
        signed = signed_wrapper(self.raw)
        damaged = bytearray(signed)
        damaged[24:32] = b"00000004"
        self.assert_unverified(bytes(damaged), "invalid_signed_wrapper_tags")
        damaged = bytearray(signed)
        damaged[16:24] = b"ffffffff"
        self.assert_unverified(bytes(damaged), "signed_wrapper_size_limit")
        for offset in (1, 8, 55, 56, 70, len(signed) - 1):
            with self.subTest(offset=offset):
                self.assert_unverified(signed[:offset])
        manifest = ("Manifest Version: 1.0\nCreate By: Fixture\nName: rootfs_openUBMC.hpm\n"
                    "SHA256-Digest: " + "0" * 64 + "\n").encode()
        self.assert_unverified(signed_wrapper(self.raw, manifest=manifest), "manifest_digest_mismatch")
        for malformed in (manifest.replace(b"SHA256-Digest:", b"Digest:"),
                          manifest + b"SHA256-Digest: " + b"0" * 64 + b"\n",
                          manifest.replace(b"Name: rootfs_openUBMC.hpm", b"Name: ../rootfs.hpm")):
            self.assert_unverified(signed_wrapper(self.raw, manifest=malformed))

    def test_picmg_header_action_and_payload_length_corruption(self) -> None:
        for offset in (10, 37, 43, 49, app_offset(self.raw) + 5):
            damaged = bytearray(self.raw)
            damaged[offset] ^= 1
            self.assert_unverified(bytes(damaged))
        damaged = bytearray(self.raw)
        struct.pack_into("<I", damaged, app_offset(self.raw) + 33, 0xffffffff)
        self.assert_unverified(bytes(damaged))
        damaged = bytearray(self.raw)
        damaged[8] = 2
        damaged[37] = (-sum(damaged[:37])) % 256
        self.assert_unverified(bytes(damaged), "unsupported_picmg_header")

    def test_duplicate_banks_actions_wrong_path_and_picmg_trailer(self) -> None:
        app = app_offset(self.raw)
        data = app + 37
        damaged = bytearray(self.raw)
        damaged[data + 288:data + 320] = damaged[data + 256:data + 288]
        self.assert_unverified(bytes(damaged), "unsupported_or_multiple_active_banks")
        damaged = bytearray(self.raw)
        damaged[data + 264] = ord("X")
        self.assert_unverified(bytes(damaged), "unexpected_bank_path")
        self.assert_unverified(self.raw + self.raw[app:], "unexpected_picmg_trailer_or_action")
        self.assert_unverified(self.raw + b"\0" * 16, "unexpected_picmg_trailer_or_action")

    def test_action_mask_rejected_even_with_valid_checksum(self) -> None:
        for offset in (38, 44, app_offset(self.raw)):
            damaged = bytearray(self.raw)
            damaged[offset + 1] ^= 1
            damaged[offset + 5] = (-sum(damaged[offset:offset + 5])) % 256
            self.assert_unverified(bytes(damaged), "unsupported_action_components")

    def test_config_padding_must_also_be_valid(self) -> None:
        damaged = bytearray(self.raw)
        damaged[app_offset(self.raw) - 17] ^= 0x80
        self.assert_unverified(bytes(damaged), "invalid_cipher_padding_or_key")

    def test_cipher_framing_iv_padding_and_short_nonfinal_plaintext(self) -> None:
        encrypted = encrypt_gpp(make_gpp(self.rootfs))
        for tail in (b"x", b"\0" * 16):
            self.assert_unverified(make_hpm(self.rootfs, encrypted=encrypted + tail))
        self.assert_unverified(make_hpm(self.rootfs, encrypted=encrypted[:270]))
        damaged = bytearray(encrypted)
        damaged[256] ^= 1
        self.assert_unverified(make_hpm(self.rootfs, encrypted=bytes(damaged)), "unsupported_cipher_iv")
        damaged = bytearray(encrypted)
        # The penultimate CBC block controls the final plaintext padding byte.
        damaged[-17] ^= 0x80
        self.assert_unverified(make_hpm(self.rootfs, encrypted=bytes(damaged)), "invalid_cipher_padding_or_key")
        # A full encoded chunk must decode to exactly 10240 bytes, never 10241.
        oversized = b"q" * 10241
        iv = b"a" + b"\0" * 15
        enc = Cipher(algorithms.AES(TEST_KEY), modes.CBC(iv)).encryptor()
        bad_record = iv + enc.update(oversized + b"\x0f" * 15) + enc.finalize()
        malformed = b"E" * 256 + bad_record + encrypted[256:]
        self.assert_unverified(make_hpm(self.rootfs, encrypted=malformed), "invalid_cipher_plaintext_length")

    def test_gpp_ranges_descriptors_and_trailers(self) -> None:
        original = make_gpp(self.rootfs)
        for off, value in ((0, 4), (20, 513), (48, 4), (52, 1), (56, 0xffffffff), (60, 1), (64, 1)):
            with self.subTest(offset=off):
                damaged = bytearray(original)
                struct.pack_into("<I", damaged, off, value)
                self.assert_unverified(make_hpm(self.rootfs, gpp=bytes(damaged)))
        self.assert_unverified(make_hpm(self.rootfs, gpp=original + b"x"), "unexpected_gpp_trailer")
        rootfs_start = struct.unpack_from("<I", original, 52)[0]
        for off, value in ((0, 0), (4, 5), (12, 1), (80, 163), (112, 4), (148, 16), (160, 0)):
            with self.subTest(suboffset=off):
                damaged = bytearray(original)
                struct.pack_into("<I", damaged, rootfs_start + off, value)
                self.assert_unverified(make_hpm(self.rootfs, gpp=bytes(damaged)))

    def test_gzip_truncation_crc_concatenation_and_compressed_bomb(self) -> None:
        compressed = gzip.compress(make_tar(self.rootfs), mtime=0)
        for malformed in (compressed[:-1], compressed + b"x", compressed + gzip.compress(b"other"),
                          compressed[:-8] + bytes((compressed[-8] ^ 1,)) + compressed[-7:]):
            self.assert_unverified(make_hpm(self.rootfs, archive=malformed))
        # Header declares the expected rootfs, but the stream inflates past the bounded tail.
        bomb = make_tar(self.rootfs) + b"\0" * (4 * 1024 * 1024)
        result = self.assert_unverified(make_hpm(self.rootfs, archive=gzip.compress(bomb)))
        self.assertIn(result["reason"], ("decompressed_size_limit", "tar_trailer_size_limit"))

    def test_tar_duplicate_extra_link_path_checksum_and_nonzero_tail(self) -> None:
        cases = [
            [("rootfs_iBMC.img", self.rootfs, tarfile.REGTYPE)] * 2,
            [("rootfs_iBMC.img", self.rootfs, tarfile.REGTYPE), ("other", b"", tarfile.REGTYPE)],
            [("rootfs_iBMC.img", b"", tarfile.SYMTYPE)],
            [("rootfs_iBMC.img", b"", tarfile.LNKTYPE)],
            [("../rootfs_iBMC.img", self.rootfs, tarfile.REGTYPE)],
            [("rootfs_iBMC.img", b"", tarfile.DIRTYPE)],
        ]
        for members in cases:
            with self.subTest(members=[(x[0], x[2]) for x in members]):
                self.assert_unverified(make_hpm(self.rootfs, archive=gzip.compress(make_tar(self.rootfs, members=members))))
        tar = bytearray(make_tar(self.rootfs))
        tar[0] ^= 1
        self.assert_unverified(make_hpm(self.rootfs, archive=gzip.compress(tar)), "tar_checksum_mismatch")
        tar = bytearray(make_tar(self.rootfs))
        tar[-1] = 1
        self.assert_unverified(make_hpm(self.rootfs, archive=gzip.compress(tar)), "unexpected_tar_trailer_or_multiple_members")
        tar = bytearray(make_tar(self.rootfs))
        tar[512 + len(self.rootfs)] = 1
        self.assert_unverified(make_hpm(self.rootfs, archive=gzip.compress(tar)), "invalid_tar_member_padding")

    def test_gnu_zero_tail_and_exact_two_zero_blocks_accepted(self) -> None:
        tar = make_tar(self.rootfs)
        end = 512 + ((len(self.rootfs) + 511) // 512) * 512
        for content in (tar, tar[:end] + b"\0" * 1024):
            result = self.verify(make_hpm(self.rootfs, archive=gzip.compress(content)))
            self.assertEqual(result["status"], "verified", result)
        self.assert_unverified(make_hpm(self.rootfs, archive=gzip.compress(tar[:end] + b"\0" * 512)))

    def test_gzip_output_and_file_reads_are_bounded(self) -> None:
        original = verifier._Region.read
        sizes = []
        def observe(region: object, offset: int, size: int) -> bytes:
            sizes.append(size)
            return original(region, offset, size)
        with mock.patch.object(verifier._Region, "read", new=observe):
            result = self.verify()
        self.assertEqual(result["status"], "verified", result)
        self.assertLessEqual(max(sizes), 65536)


if __name__ == "__main__":
    unittest.main()
