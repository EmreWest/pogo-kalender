"""Discord collector: import, storage, duplicates, limits and resilience."""

from __future__ import annotations

import io
import json
import logging
import unittest
from pathlib import Path

from tests import helpers
from tests.helpers import (
    CHANNEL_ID,
    GIF,
    GUILD_ID,
    JPEG,
    PDF,
    PNG,
    WEBHOOK_ID,
    FakeDiscordClient,
    FakeDownloader,
    attachment,
    message,
)

import discord_worker
from discord_worker import (
    DiscordAPIError,
    DiscordClient,
    DiscordConfigError,
    InfographicCollector,
    TokenFilter,
    discord_environment,
    host_allowed,
)
from infographic_store import InfographicStore, infographic_settings


class CollectorTestCase(unittest.TestCase):
    allowed_authors: set[str] = {WEBHOOK_ID}

    def setUp(self) -> None:
        self.data_dir = helpers.fresh_data_dir(self.id().rsplit(".", 1)[-1])
        self.config = helpers.make_config(self.data_dir)
        self.settings = infographic_settings(self.config)
        self.store = InfographicStore.from_settings(self.settings)
        self.client = FakeDiscordClient()
        self.downloader = FakeDownloader()
        self.ocr_calls: list[Path] = []

    def collector(self, **kwargs) -> InfographicCollector:
        return InfographicCollector(
            self.settings,
            self.store,
            kwargs.get("client", self.client),
            GUILD_ID,
            [CHANNEL_ID],
            kwargs.get("authors", self.allowed_authors),
            downloader=self.downloader,
            ocr_func=kwargs.get("ocr_func"),
            state_file=self.settings["worker_state_file"],
        )


class NewMessagesTest(CollectorTestCase):
    def test_01_new_message_with_jpg_attachment(self) -> None:
        collector = self.collector()
        created = collector.process_message(CHANNEL_ID, message("1300000000000000001", "Giratina Raids", [attachment("1400000000000000001")]))
        self.assertEqual(created, ["1300000000000000001-1400000000000000001"])
        item = self.store.get(created[0])
        self.assertEqual(item["attachment"]["content_type"], "image/jpeg")
        self.assertEqual(item["attachment"]["original_filename"], "infografik.jpg")
        self.assertEqual(item["attachment"]["width"], 1932)
        self.assertEqual(item["matching"]["status"], "pending")
        self.assertEqual(item["attribution"]["creator"], "G47IX")
        # Link to the original message in Galix' server (crosspost reference)
        self.assertIn("/999999999999999999/888888888888888888/777777777777777777", item["discord"]["message_url"])
        self.assertNotIn("?", item["attachment"]["original_url"])
        path = self.store.resolve_file(item["attachment"]["local_path"])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), JPEG)
        self.assertRegex(item["attachment"]["local_path"], r"^2026/11/1300000000000000001-1400000000000000001\.jpg$")

    def test_02_message_with_multiple_images(self) -> None:
        self.downloader.files["1400000000000000003"] = PNG
        created = self.collector().process_message(
            CHANNEL_ID,
            message("1300000000000000002", "", [attachment("1400000000000000002"), attachment("1400000000000000003", "teil2.png", content_type="image/png")]),
        )
        self.assertEqual(len(created), 2)
        types = sorted(self.store.get(record_id)["attachment"]["content_type"] for record_id in created)
        self.assertEqual(types, ["image/jpeg", "image/png"])

    def test_03_message_without_image(self) -> None:
        created = self.collector().process_message(CHANNEL_ID, message("1300000000000000003", "Nur Text, kein Bild"))
        self.assertEqual(created, [])
        self.assertEqual(self.store.items(), [])
        self.assertEqual(self.downloader.calls, [])

    def test_ocr_text_is_stored(self) -> None:
        collector = self.collector(ocr_func=lambda path: "4. NOV - 8. NOV\nGIRATINA")
        created = collector.process_message(CHANNEL_ID, message("1300000000000000004", "", [attachment("1400000000000000004")]))
        item = self.store.get(created[0])
        self.assertEqual(item["ocr"]["status"], "done")
        self.assertIn("GIRATINA", item["ocr"]["text"])

    def test_ocr_failure_does_not_block_import(self) -> None:
        def broken(_path):
            raise RuntimeError("tesseract kaputt")

        created = self.collector(ocr_func=broken).process_message(
            CHANNEL_ID, message("1300000000000000005", "", [attachment("1400000000000000005")])
        )
        item = self.store.get(created[0])
        self.assertEqual(item["ocr"]["status"], "error")
        self.assertEqual(item["matching"]["status"], "pending")

    def test_foreign_author_is_ignored(self) -> None:
        created = self.collector().process_message(
            CHANNEL_ID, message("1300000000000000006", "", [attachment("1400000000000000006")], author_id="444444444444444444", webhook=False)
        )
        self.assertEqual(created, [])

    def test_embed_image_from_discord_cdn(self) -> None:
        embed = {"type": "image", "url": "https://cdn.discordapp.com/x.png", "thumbnail": {"url": "https://cdn.discordapp.com/attachments/1/2/bild.png", "proxy_url": "https://media.discordapp.net/attachments/1/2/bild.png"}}
        foreign = {"type": "image", "thumbnail": {"url": "https://example.com/bild.png"}}
        self.downloader.default = PNG
        created = self.collector().process_message(CHANNEL_ID, message("1300000000000000007", "", embeds=[embed, foreign]))
        self.assertEqual(len(created), 1)
        self.assertTrue(all("example.com" not in url for url in self.downloader.calls))

    def test_edited_message_updates_text(self) -> None:
        collector = self.collector()
        original = message("1300000000000000008", "Alter Text", [attachment("1400000000000000008")])
        created = collector.process_message(CHANNEL_ID, original)
        edited = message("1300000000000000008", "Neuer Text 24. Nov", [attachment("1400000000000000008")], edited="2026-11-03T13:00:00+00:00")
        self.assertEqual(collector.process_message(CHANNEL_ID, edited), [])
        item = self.store.get(created[0])
        self.assertEqual(item["discord"]["content"], "Neuer Text 24. Nov")
        self.assertTrue(item["matching"].get("needs_rematch"))
        self.assertEqual(len(self.downloader.calls), 1)


class DuplicatesAndLimitsTest(CollectorTestCase):
    def test_12_duplicate_message_is_not_stored_twice(self) -> None:
        collector = self.collector()
        msg = message("1300000000000000010", "", [attachment("1400000000000000010")])
        self.assertEqual(len(collector.process_message(CHANNEL_ID, msg)), 1)
        self.assertEqual(collector.process_message(CHANNEL_ID, msg), [])
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(len(self.downloader.calls), 1)
        # Same image bytes posted again in a new message: detected by SHA-256
        repost = message("1300000000000000011", "", [attachment("1400000000000000011")])
        self.assertEqual(collector.process_message(CHANNEL_ID, repost), [])
        self.assertEqual(len(self.store.items()), 1)
        self.assertIn("Duplikat", self.store.read()["skipped"]["1300000000000000011-1400000000000000011"])
        files = [p for p in Path(self.settings["storage_dir"]).rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)

    def test_13_invalid_file_type(self) -> None:
        collector = self.collector()
        gif = message("1300000000000000020", "", [attachment("1400000000000000020", "animiert.gif", content_type="image/gif")])
        self.assertEqual(collector.process_message(CHANNEL_ID, gif), [])
        self.assertEqual(self.downloader.calls, [])
        # Disguised file: .jpg name but PDF content
        self.downloader.files["1400000000000000021"] = PDF
        fake = message("1300000000000000021", "", [attachment("1400000000000000021", "bild.jpg")])
        self.assertEqual(collector.process_message(CHANNEL_ID, fake), [])
        self.assertEqual(self.store.items(), [])
        skipped = self.store.read()["skipped"]
        self.assertIn("1300000000000000020-1400000000000000020", skipped)
        self.assertIn("1300000000000000021-1400000000000000021", skipped)
        self.assertEqual([p for p in Path(self.settings["storage_dir"]).rglob("*") if p.is_file()], [])

    def test_14_file_too_large(self) -> None:
        collector = self.collector()
        declared = message("1300000000000000030", "", [attachment("1400000000000000030", size=5 * 1024 * 1024)])
        self.assertEqual(collector.process_message(CHANNEL_ID, declared), [])
        self.assertEqual(self.downloader.calls, [])
        # Size not declared correctly: the downloaded bytes are checked again
        self.downloader.files["1400000000000000031"] = JPEG + b"\x00" * (2 * 1024 * 1024)
        undeclared = message("1300000000000000031", "", [attachment("1400000000000000031", size=1000)])
        self.assertEqual(collector.process_message(CHANNEL_ID, undeclared), [])
        self.assertEqual(self.store.items(), [])

    def test_download_only_from_allowed_hosts(self) -> None:
        hosts = ["cdn.discordapp.com", "media.discordapp.net"]
        self.assertTrue(host_allowed("https://cdn.discordapp.com/attachments/1/2/a.jpg", hosts))
        self.assertFalse(host_allowed("http://cdn.discordapp.com/a.jpg", hosts))
        self.assertFalse(host_allowed("https://cdn.discordapp.com.evil.example/a.jpg", hosts))
        self.assertFalse(host_allowed("https://169.254.169.254/latest", hosts))
        self.assertFalse(host_allowed("https://cdn.discordapp.com:8443/a.jpg", hosts))
        with self.assertRaises(ValueError):
            discord_worker.download_image("https://example.com/a.jpg", hosts, 1000, 5)


class PollingTest(CollectorTestCase):
    def test_historical_import_is_limited(self) -> None:
        self.settings["historical_import"]["message_limit"] = 3
        self.client.messages = [
            message(str(1300000000000000100 + i), "", [attachment(str(1400000000000000100 + i))]) for i in range(6)
        ]
        self.downloader.files = {str(1400000000000000100 + i): JPEG + bytes([i]) for i in range(6)}
        collector = self.collector()
        collector.poll_once()
        self.assertEqual(len(self.store.items()), 3)
        self.assertEqual(sorted(i["discord"]["message_id"] for i in self.store.items())[0], "1300000000000000103")
        state = json.loads(Path(self.settings["worker_state_file"]).read_text())
        self.assertTrue(state["channels"][CHANNEL_ID]["historical_done"])
        self.assertEqual(state["channels"][CHANNEL_ID]["last_message_id"], "1300000000000000105")

    def test_15_connection_loss_is_handled(self) -> None:
        collector = self.collector()
        self.client.fail_with = ConnectionError("Discord nicht erreichbar")
        result = collector.poll_once()
        self.assertEqual(result["status"], "error")
        self.assertIn("nicht erreichbar", result["last_error"])
        state = json.loads(Path(self.settings["worker_state_file"]).read_text())
        self.assertEqual(state["status"], "error")
        # Next cycle works again
        self.client.fail_with = None
        self.client.messages = [message("1300000000000000200", "", [attachment("1400000000000000200")])]
        result = collector.poll_once()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.store.items()), 1)

    def test_15b_download_error_is_retried(self) -> None:
        collector = self.collector()
        self.downloader.fail = ConnectionError("Timeout")
        msg = message("1300000000000000210", "", [attachment("1400000000000000210")])
        collector.process_message(CHANNEL_ID, msg)
        item = self.store.items()[0]
        self.assertEqual(item["matching"]["status"], "error")
        self.downloader.fail = None
        collector.process_message(CHANNEL_ID, msg)
        item = self.store.items()[0]
        self.assertEqual(item["matching"]["status"], "pending")
        self.assertTrue(item["attachment"]["local_path"])

    def test_rate_limit_and_server_errors_are_retried(self) -> None:
        responses = [
            (429, {}, b'{"retry_after": 0.01, "global": false}'),
            (502, {}, b""),
            (200, {"x-ratelimit-remaining": "4"}, b'[{"id": "1300000000000000300"}]'),
        ]
        sleeps: list[float] = []
        seen_headers: list[dict[str, str]] = []

        def transport(url, headers, timeout):
            seen_headers.append(headers)
            return responses.pop(0)

        client = DiscordClient("geheimes.token.wert", transport=transport, sleep=sleeps.append)
        result = client.get_messages(CHANNEL_ID, limit=10)
        self.assertEqual(result[0]["id"], "1300000000000000300")
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(seen_headers[0]["Authorization"], "Bot geheimes.token.wert")
        self.assertIn("DiscordBot", seen_headers[0]["User-Agent"])

        def forbidden(url, headers, timeout):
            return 403, {}, b'{"message": "Missing Access", "code": 50001}'

        with self.assertRaises(DiscordAPIError) as ctx:
            DiscordClient("x", transport=forbidden, sleep=lambda _s: None).get_channel(CHANNEL_ID)
        self.assertEqual(ctx.exception.status, 403)
        self.assertNotIn("geheimes", str(ctx.exception))

    def test_16_restart_resumes_without_reprocessing(self) -> None:
        self.client.messages = [message("1300000000000000400", "", [attachment("1400000000000000400")])]
        first = self.collector()
        first.poll_once()
        self.assertEqual(len(self.store.items()), 1)
        downloads = len(self.downloader.calls)

        # "Container restart": new collector instance, same volume
        self.client.messages.append(message("1300000000000000401", "", [attachment("1400000000000000401")]))
        self.downloader.files["1400000000000000401"] = PNG
        second = self.collector()
        second.poll_once()
        self.assertEqual(len(self.store.items()), 2)
        self.assertEqual(len(self.downloader.calls), downloads + 1)
        after_calls = [c for c in self.client.calls if c[1]["after"]]
        self.assertTrue(after_calls)
        self.assertEqual(after_calls[-1][1]["after"], "1300000000000000400")

    def test_wrong_guild_is_refused(self) -> None:
        client = FakeDiscordClient(guild_id="555555555555555555")
        client.messages = [message("1300000000000000500", "", [attachment("1400000000000000500")])]
        result = self.collector(client=client).poll_once()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.items(), [])


class EnvironmentTest(unittest.TestCase):
    def test_environment_validation(self) -> None:
        with self.assertRaises(DiscordConfigError):
            discord_environment({})
        with self.assertRaises(DiscordConfigError):
            discord_environment({"DISCORD_BOT_TOKEN": "t", "DISCORD_GUILD_ID": "abc", "DISCORD_CHANNEL_IDS": "1"})
        env = discord_environment({
            "DISCORD_BOT_TOKEN": "token",
            "DISCORD_GUILD_ID": GUILD_ID,
            "DISCORD_CHANNEL_IDS": f"{CHANNEL_ID}, 222222222222222223",
            "DISCORD_ALLOWED_AUTHOR_IDS": WEBHOOK_ID,
        })
        self.assertEqual(env["channel_ids"], [CHANNEL_ID, "222222222222222223"])
        self.assertEqual(env["allowed_author_ids"], {WEBHOOK_ID})

    def test_token_never_reaches_log_output(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        token = "MTIzNDU2Nzg5.geheim.tokenwert"
        handler.addFilter(TokenFilter(token))
        logger = logging.getLogger("token-test")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.error("Fehler mit %s im Text", token)
        self.assertNotIn(token, stream.getvalue())
        self.assertIn("***", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
