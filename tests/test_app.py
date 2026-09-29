import io
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import app
from app import (
    ChatbotHandler,
    create_reply,
    find_names,
    initialize_database,
    load_intents,
    lookup_name,
)


class NameSuggestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.intents = load_intents()
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = Path(cls.temporary_directory.name) / "kids_names.db"
        initialize_database(cls.database_path)

    @classmethod
    def tearDownClass(cls):
        cls.temporary_directory.cleanup()

    def reply(self, message):
        return create_reply(message, self.intents, self.database_path)

    def test_suggests_names_from_local_database(self):
        result = self.reply("Suggest baby names")
        self.assertEqual(result["tag"], "name_suggestions")
        self.assertEqual(len(result["suggestions"]), 5)
        self.assertTrue(all("name" in name and "meaning" in name for name in result["suggestions"]))

    def test_filters_name_suggestions_by_gender_and_style(self):
        result = self.reply("Suggest girl nature names")
        self.assertEqual(result["tag"], "name_suggestions")
        self.assertTrue(
            all(
                entry["gender"] == "girl" and entry["style"] == "nature"
                for entry in result["suggestions"]
            )
        )

    def test_filters_names_by_starting_letter(self):
        result = self.reply("Suggest names starting with A")
        self.assertEqual(result["tag"], "name_suggestions")
        self.assertTrue(all(entry["name"].startswith("A") for entry in result["suggestions"]))

    def test_combined_filters_with_no_matches_are_explained(self):
        result = self.reply("Suggest boy nature names starting with Z")
        self.assertEqual(result["tag"], "no_name_suggestions")
        self.assertIn("couldn't find", result["reply"])

    def test_looks_up_name_meaning(self):
        result = self.reply("What does Aurora mean?")
        self.assertEqual(result["tag"], "name_meaning")
        self.assertEqual(result["name"]["name"], "Aurora")
        self.assertIn("Dawn", result["name"]["meaning"])

    def test_name_lookup_is_case_insensitive(self):
        self.assertEqual(lookup_name("KAI", self.database_path)["name"], "Kai")

    def test_unknown_name_has_clear_response(self):
        result = self.reply("What does Zephaniah mean?")
        self.assertEqual(result["tag"], "name_not_found")
        self.assertIn("Zephaniah", result["reply"])

    def test_greetings_still_work(self):
        self.assertEqual(self.reply("hello")["tag"], "greeting")

    def test_database_initialization_is_repeatable(self):
        initialize_database(self.database_path)
        self.assertGreaterEqual(len(find_names(self.database_path, limit=500)), 100)


class ChatbotHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.previous_database_path = app.DATABASE_PATH
        app.DATABASE_PATH = Path(cls.temporary_directory.name) / "kids_names.db"
        initialize_database(app.DATABASE_PATH)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ChatbotHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        app.DATABASE_PATH = cls.previous_database_path
        cls.temporary_directory.cleanup()

    def post_json(self, payload):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = json.dumps(payload)
        connection.request(
            "POST",
            "/api/chat",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        status = response.status
        connection.close()
        return status, result

    def test_chat_endpoint_returns_name_suggestions(self):
        status, result = self.post_json({"message": "Suggest neutral names"})
        self.assertEqual(status, 200)
        self.assertEqual(result["tag"], "name_suggestions")
        self.assertTrue(
            all(entry["gender"] == "neutral" for entry in result["suggestions"])
        )

    def test_chat_endpoint_rejects_blank_message(self):
        status, result = self.post_json({"message": "  "})
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_homepage_is_served(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("Kids' Name Prediction", page)
        self.assertIn("/api/chat", page)

    def test_vercel_wsgi_entrypoint_returns_name_suggestions(self):
        body = json.dumps({"message": "Suggest girl names"}).encode("utf-8")
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/chat",
            "CONTENT_TYPE": "application/json; charset=utf-8",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        response_status = []
        headers = []
        response_body = b"".join(
            app.app(environ, lambda status, response_headers: (
                response_status.append(status),
                headers.extend(response_headers),
            ))
        )
        result = json.loads(response_body)
        self.assertTrue(response_status[0].startswith("200 "))
        self.assertEqual(result["tag"], "name_suggestions")
        self.assertTrue(
            any(name == "Content-Type" and value.startswith("application/json")
                for name, value in headers)
        )

    def test_vercel_wsgi_entrypoint_serves_homepage(self):
        response_status = []
        response_body = b"".join(
            app.app(
                {"REQUEST_METHOD": "GET", "PATH_INFO": "/"},
                lambda status, headers: response_status.append(status),
            )
        )
        self.assertTrue(response_status[0].startswith("200 "))
        self.assertIn(b"Kids' Name Prediction", response_body)


if __name__ == "__main__":
    unittest.main()
