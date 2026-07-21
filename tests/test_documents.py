import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from config.settings import settings
from web import auth, database
from web.app import app


class TestVersionedDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_PATH = Path(self.tmp.name) / "documents.db"
        database.init_db()
        self.original_secure = settings.secure_cookies
        settings.secure_cookies = False
        with database.connect() as db:
            db.execute(
                "UPDATE users SET username=?,password_hash=? WHERE id='default'",
                ("doc-user", auth.hash_password("test-pass")),
            )
        self.client = TestClient(app)
        response = self.client.post(
            "/api/auth/login", json={"username": "doc-user", "password": "test-pass"}
        )
        self.assertEqual(response.status_code, 200)

    def tearDown(self):
        settings.secure_cookies = self.original_secure
        self.tmp.cleanup()

    def test_document_is_versioned_exportable_and_restored_with_chat(self):
        session = self.client.post(
            "/api/chat/sessions", json={"title": "写 Listing", "agent": "listing"}
        ).json()
        message = self.client.post(
            f"/api/chat/sessions/{session['id']}/messages",
            json={"role": "assistant", "content": "# Title\nOriginal copy"},
        ).json()

        created = self.client.post(
            "/api/documents",
            json={
                "doc_type": "listing_copy",
                "title": "Listing 文案",
                "content": "# Title\nOriginal copy",
                "session_id": session["id"],
                "source_message_id": message["id"],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        document = created.json()
        self.assertEqual(document["current_version"], 1)

        updated = self.client.patch(
            f"/api/documents/{document['id']}",
            json={"title": "Listing 文案 v2", "content": "# Title\nEdited copy"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["current_version"], 2)

        versions = self.client.get(f"/api/documents/{document['id']}/versions").json()["items"]
        self.assertEqual([item["version"] for item in versions], [2, 1])
        self.assertEqual(versions[1]["content"], "# Title\nOriginal copy")

        chat = self.client.get(f"/api/chat/sessions/{session['id']}").json()
        self.assertEqual(chat["messages"][0]["document"]["current_version"], 2)
        self.assertEqual(chat["messages"][0]["document"]["content"], "# Title\nEdited copy")

        exported = self.client.get(f"/api/documents/{document['id']}/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["content-disposition"])
        self.assertIn("Edited copy", exported.text)


if __name__ == "__main__":
    unittest.main()
