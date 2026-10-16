import unittest

from app.callbacks import CallbackError, decode, encode


class CallbackTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(decode(encode("start", "abc123", "1")), ("start", ("abc123", "1")))

    def test_rejects_oversize_payload(self):
        with self.assertRaises(CallbackError):
            encode("menu", "x" * 60)

    def test_rejects_unsafe_characters(self):
        with self.assertRaises(CallbackError):
            encode("menu", "hello world")
        with self.assertRaises(CallbackError):
            decode("v1:menu:hello world")

    def test_rejects_unknown_version(self):
        with self.assertRaises(CallbackError):
            decode("v0:menu:home")


if __name__ == "__main__":
    unittest.main()
