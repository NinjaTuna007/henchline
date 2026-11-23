"""The phone's speech-to-text files: what the laptop prepares, lists and serves (voice/phoneasr.py, call/server.py)."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # first: a throwaway HOME
from voice import phoneasr

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
    from call import server
except ImportError:  # the web server's packages aren't in this Python
    AioHTTPTestCase = None

assert sandbox.HOME


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fill(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in phoneasr.FILES:
        (root / name).write_bytes(name.encode())


def pin_filled(case: unittest.TestCase) -> None:
    """PINNED as the hashes of what fill() writes, for this test."""
    patch = mock.patch.object(phoneasr, "PINNED", {n: sha(n.encode()) for n in phoneasr.PINNED})
    patch.start()
    case.addCleanup(patch.stop)


class Model(unittest.TestCase):
    def setUp(self):
        pin_filled(self)

    def test_the_files_are_the_transducer_its_vocab_the_vad_and_a_card(self):
        for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt", "bpe.vocab",
                     "silero_vad.onnx", "MODEL_CARD"):
            self.assertIn(name, phoneasr.FILES)
        self.assertEqual(set(phoneasr.PINNED), set(phoneasr.FILES) - {"MODEL_CARD"})
        self.assertEqual(phoneasr.FILES[-1], "encoder.int8.onnx")  # the big one last

    def test_manifest_hashes_each_file_and_carries_the_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(phoneasr.manifest(root, hints=[]))  # not prepared yet
            fill(root)
            found = phoneasr.manifest(root, hints=["Claude", "Berzelius"])
            self.assertEqual(found["version"], phoneasr.VERSION)
            self.assertEqual(found["engine"], phoneasr.ENGINE)
            self.assertEqual([f["name"] for f in found["files"]], list(phoneasr.FILES))
            self.assertEqual(found["files"][0], {"name": "tokens.txt", "size": 10, "sha256": sha(b"tokens.txt")})
            self.assertEqual(found["size"], sum(len(n) for n in phoneasr.FILES))
            self.assertEqual(found["hints"], ["Claude", "Berzelius"])
            with mock.patch.object(phoneasr, "_sha256", side_effect=AssertionError("hashed again")):
                self.assertEqual(phoneasr.manifest(root, hints=["Claude", "Berzelius"]), found)  # kept hashes
            (root / "MODEL_CARD").write_bytes(b"a newer card")  # not pinned: listed with its new hash
            self.assertEqual(phoneasr.manifest(root, hints=[])["files"][2]["sha256"], sha(b"a newer card"))
            (root / "tokens.txt").write_bytes(b"changed!!!!")  # not the pinned file: never offered
            self.assertIsNone(phoneasr.manifest(root, hints=[]))

    def test_one_file_missing_is_not_prepared(self):
        with tempfile.TemporaryDirectory() as tmp:
            fill(Path(tmp))
            (Path(tmp) / "silero_vad.onnx").unlink()
            self.assertIsNone(phoneasr.manifest(Path(tmp), hints=[]))

    def test_only_listed_names_are_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fill(root)
            (root / "hotwords.txt").write_text("x")
            self.assertEqual(phoneasr.model_file("encoder.int8.onnx", root), root / "encoder.int8.onnx")
            for bad in ("hotwords.txt", ".manifest.json", "../encoder.int8.onnx", "encoder.int8.onnx.part", "",
                        "nemotron/encoder.int8.onnx"):
                self.assertIsNone(phoneasr.model_file(bad, root), bad)
            (root / "joiner.int8.onnx").unlink()
            self.assertIsNone(phoneasr.model_file("joiner.int8.onnx", root))


class Pins(unittest.TestCase):
    def test_every_file_but_the_card_is_pinned(self):
        self.assertEqual(set(phoneasr.PINNED), set(phoneasr.FILES) - {"MODEL_CARD"})
        self.assertTrue(all(len(h) == 64 for h in phoneasr.PINNED.values()))
        self.assertIn("NVIDIA Open Model License", phoneasr.CARD)


class Hints(unittest.TestCase):
    def test_cleaned_to_single_lines_without_repeats(self):
        self.assertEqual(phoneasr.clean_hints(["Claude", " Claude ", "two\nlines", "", 3, None, "x" * 80]),
                         ["Claude", "two lines", "x" * 60])
        self.assertEqual(phoneasr.clean_hints("Claude"), [])
        self.assertEqual(len(phoneasr.clean_hints([f"w{i}" for i in range(500)])), phoneasr.MAX_HINTS)

    def test_from_the_config_else_the_defaults(self):
        from voice.speech import whisper
        whisper.CONFIG.unlink(missing_ok=True)
        self.assertEqual(phoneasr.hint_words(), whisper.DEFAULT_HOTWORDS)
        whisper.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        try:
            whisper.CONFIG.write_text(json.dumps({"hotwords": ["Henchline", "Aquiferrous"]}))
            self.assertEqual(phoneasr.hint_words(), ["Henchline", "Aquiferrous"])
            whisper.CONFIG.write_text("{not json")
            self.assertEqual(phoneasr.hint_words(), whisper.DEFAULT_HOTWORDS)
        finally:
            whisper.CONFIG.unlink(missing_ok=True)


class Prepare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data, self.root = Path(self.tmp.name) / "handsfree", Path(self.tmp.name) / "phone-asr"
        (self.data / "nemotron").mkdir(parents=True)
        self.pinned = {}
        for name in phoneasr.PINNED:
            body = f"the laptop's {name}".encode()
            where = self.data / name if name == "silero_vad.onnx" else self.data / "nemotron" / name
            where.write_bytes(body)
            self.pinned[name] = sha(body)
        patch = mock.patch.object(phoneasr, "PINNED", self.pinned)
        patch.start()
        self.addCleanup(patch.stop)

    def test_links_the_laptops_own_files_and_writes_the_card(self):
        target = phoneasr.prepare(self.root, self.data)
        self.assertEqual(target, self.root / phoneasr.VERSION)
        for name in self.pinned:
            self.assertEqual(sha((target / name).read_bytes()), self.pinned[name])
        self.assertIn("Silero", (target / "MODEL_CARD").read_text())
        self.assertEqual((target / "encoder.int8.onnx").stat().st_ino,
                         (self.data / "nemotron/encoder.int8.onnx").stat().st_ino)  # no second copy on one disk
        self.assertFalse(list(target.glob("*.part")))
        self.assertIsNotNone(phoneasr.manifest(target, hints=[]))

    def test_copies_when_it_cannot_link(self):
        with mock.patch.object(phoneasr.os, "link", side_effect=OSError("another disk")):
            target = phoneasr.prepare(self.root, self.data)
        self.assertNotEqual((target / "encoder.int8.onnx").stat().st_ino,
                            (self.data / "nemotron/encoder.int8.onnx").stat().st_ino)
        self.assertEqual(sha((target / "encoder.int8.onnx").read_bytes()), self.pinned["encoder.int8.onnx"])

    def test_a_file_that_isnt_the_pinned_one_is_refused(self):
        (self.data / "nemotron/joiner.int8.onnx").write_bytes(b"another model")
        with self.assertRaises(RuntimeError) as caught:
            phoneasr.prepare(self.root, self.data)
        self.assertIn("joiner.int8.onnx", str(caught.exception))
        self.assertFalse((self.root / phoneasr.VERSION / "joiner.int8.onnx").exists())

    def test_a_wrong_file_already_there_is_replaced(self):
        target = self.root / phoneasr.VERSION
        target.mkdir(parents=True)
        (target / "tokens.txt").write_bytes(b"stale")
        phoneasr.prepare(self.root, self.data)
        self.assertEqual(sha((target / "tokens.txt").read_bytes()), self.pinned["tokens.txt"])


@unittest.skipIf(AioHTTPTestCase is None, "needs aiohttp")
class Routes(AioHTTPTestCase if AioHTTPTestCase else unittest.TestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_get("/phone-asr/manifest", server.phone_asr_manifest)
        app.router.add_get("/phone-asr/file/{name}", server.phone_asr_file)
        return app

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.object(phoneasr, "MODEL_DIR", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        pin_filled(self)

    def origin(self):
        return {"Origin": f"https://{self.server.host}:{self.server.port}"}

    async def test_another_sites_page_is_refused(self):
        fill(self.root)
        for path in ("/phone-asr/manifest", "/phone-asr/file/tokens.txt"):
            answer = await self.client.get(path, headers={"Origin": "https://evil.example"})
            self.assertEqual(answer.status, 403, path)

    async def test_not_prepared_says_how(self):
        answer = await self.client.get("/phone-asr/manifest", headers=self.origin())
        self.assertEqual(answer.status, 404)
        self.assertIn("voice.phoneasr prepare", (await answer.json())["error"])

    async def test_a_file_that_isnt_the_pinned_one_is_refused(self):
        fill(self.root)
        (self.root / "encoder.int8.onnx").write_bytes(b"another model")
        answer = await self.client.get("/phone-asr/manifest", headers=self.origin())
        self.assertEqual(answer.status, 404)
        self.assertIn("pinned", (await answer.json())["error"])

    async def test_the_manifest_and_a_file_resumed_from_a_range(self):
        fill(self.root)
        with mock.patch.object(phoneasr, "hint_words", return_value=["Claude"]):
            answer = await self.client.get("/phone-asr/manifest", headers=self.origin())
        found = await answer.json()
        self.assertEqual(found["version"], phoneasr.VERSION)
        self.assertEqual(found["hints"], ["Claude"])
        whole = await self.client.get("/phone-asr/file/encoder.int8.onnx", headers=self.origin())
        self.assertEqual(await whole.read(), b"encoder.int8.onnx")
        rest = await self.client.get("/phone-asr/file/encoder.int8.onnx",
                                     headers={**self.origin(), "Range": "bytes=8-"})
        self.assertEqual(rest.status, 206)
        self.assertEqual(await rest.read(), b"int8.onnx")

    async def test_nothing_else_is_served(self):
        fill(self.root)
        (self.root / ".manifest.json").write_text("{}")
        for name in (".manifest.json", "secret", "..%2Fsecret"):
            answer = await self.client.get(f"/phone-asr/file/{name}", headers=self.origin())
            self.assertEqual(answer.status, 404, name)


if __name__ == "__main__":
    unittest.main()
