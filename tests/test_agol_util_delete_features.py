import unittest
from unittest.mock import patch

from agol.agol_util import AGOLDataLoader


class DeleteFeaturesPayloadTests(unittest.TestCase):
    def test_accepts_delete_payloads_with_deletes_key(self):
        with patch.object(AGOLDataLoader, "_authenticate", return_value="test-token"):
            loader = AGOLDataLoader(url="https://example.com", layer=3)

        payload = {"deletes": [123, 456]}

        with patch("requests.post") as mock_post:
            mock_post.return_value.text = "{}"
            mock_post.return_value.json.return_value = {
                "deleteResults": [
                    {"success": True, "objectId": 123},
                    {"success": True, "objectId": 456},
                ]
            }

            result = loader.delete_features(payload)

        self.assertTrue(result["success"])
        self.assertEqual(result["objectids"], [123, 456])


if __name__ == "__main__":
    unittest.main()
